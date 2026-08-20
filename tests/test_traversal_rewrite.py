"""`OverlayQuery` rewrites cross-view traversals.

`filter(customer__city='x')` across two overlay models becomes
`filter(customer_id__in=<subquery>)`, which compiles to `= ANY (ARRAY(...))`.
Measured at 900,000 view rows: 6,199.5ms against 6.3ms.

A join and a semi-join are only interchangeable under conditions the rewrite
has to enforce, so nearly all of this file is **differential**: build the same
queryset with the rewrite on and off, and assert both return exactly the same
rows. A test that only checks the SQL would not catch the failure mode that
matters, which is silently different results.

Every case also asserts *whether* the rewrite fired, so nothing can pass
vacuously by quietly not rewriting.
"""

from unittest import mock

import pytest
from django.core.exceptions import FieldError, ImproperlyConfigured
from django.db import connection, models
from django.db.models import Count, F, OuterRef, Q, Subquery
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from django_overlay.fields import OverlayFencedIn
from tests.testapp.models import (
    Member,
    Person,
    PlainPerson,
    Roster,
    WideCustomer,
    WideOrder,
    WideOrderLine,
    WideProduct,
    WideRegion,
)
from tests.testapp_shared.models import MemberSource, RosterSource, WideCustomerSource


pytestmark = pytest.mark.django_db

OFF = override_settings(DJANGO_OVERLAY_REWRITE_TRAVERSALS=False)


def sql_of(queryset) -> str:
    return str(queryset.query)


def rows(result):
    """A comparable, order-independent snapshot of whatever a build returned.

    Iterating is also what forces a queryset to execute, so the capture below
    sees the real SQL rather than an unevaluated queryset."""
    if isinstance(result, (int, float, bool, type(None))):
        return result
    if isinstance(result, dict):
        return {key: rows(value) for key, value in result.items()}
    return sorted(str(item) for item in result)


def run(build):
    """(snapshot, executed SQL) — the SQL actually sent, not str(query).

    Comparing executed SQL rather than `str(queryset.query)` means this works
    uniformly for `count()`, `exists()`, `aggregate()` and plain lists, and it
    proves the rewrite reached the real execution path."""
    with CaptureQueriesContext(connection) as captured:
        snapshot = rows(build())
    return snapshot, [query["sql"] for query in captured]


def same(build, *, expect_rewrite):
    """The heart of this file: identical results with the rewrite on and off."""
    with OFF:
        plain, plain_sql = run(build)
    rewritten, rewritten_sql = run(build)

    assert rewritten == plain, f"the rewrite changed the result\n  off: {plain}\n  on : {rewritten}"
    changed = rewritten_sql != plain_sql
    assert changed is expect_rewrite, (
        f"expected rewrite={expect_rewrite}, got {changed}\n  off: {plain_sql}\n  on : {rewritten_sql}"
    )
    return plain


# --------------------------------------------------------------- fixtures


@pytest.fixture
def graph():
    """A deliberately awkward graph: source-backed and organic rows on both
    sides, a null foreign key, a null on the far side's own column, and a
    customer nobody references."""
    region = WideRegion.objects.create(name="region7", country="GB")
    other_region = WideRegion.objects.create(name="region8", country="US")

    vendor_source = WideCustomerSource.objects.create(
        first_name="v",
        last_name="vendor",
        email="v@x",
        age=40,
        city="city42",
        postcode="pc1",
        status="active",
        score=10,
        registered_on="2020-01-01",
        notes="",
        region_id=region.id,
    )
    vendor = WideCustomer.objects.get(pk=-vendor_source.id)

    organic = WideCustomer.objects.create(
        first_name="o",
        last_name="organic",
        email="o@x",
        age=41,
        city="city42",
        postcode="pc2",
        status="lapsed",
        score=None,
        registered_on="2020-01-02",
        region=other_region,
    )
    elsewhere = WideCustomer.objects.create(
        first_name="e",
        last_name="elsewhere",
        email="e@x",
        age=42,
        city="city99",
        postcode="pc3",
        status="active",
        score=30,
        registered_on="2020-01-03",
        region=None,
    )
    unreferenced = WideCustomer.objects.create(
        first_name="u",
        last_name="unref",
        email="u@x",
        age=43,
        city="city42",
        postcode="pc4",
        status="closed",
        score=40,
        registered_on="2020-01-04",
        region=region,
    )

    product = WideProduct.objects.create(
        sku="SKU1", name="p", category="cat7", price_cents=100, weight_grams=1, supplier="s"
    )
    orders = {}
    for label, customer in (("vendor", vendor), ("organic", organic), ("elsewhere", elsewhere)):
        orders[label] = WideOrder.objects.create(
            reference=f"REF-{label}",
            status="new",
            total_cents=100,
            placed_on="2021-01-01",
            channel="web",
            currency="GBP",
            customer=customer,
        )
    for label, order in orders.items():
        WideOrderLine.objects.create(
            quantity=1, unit_price_cents=10, note=f"line-{label}", order=order, product=product
        )

    return {
        "vendor": vendor,
        "organic": organic,
        "elsewhere": elsewhere,
        "unreferenced": unreferenced,
        "orders": orders,
        "region": region,
        "product": product,
    }


# ------------------------------------------------- it rewrites, and correctly


def test_one_hop(graph):
    matched = same(
        lambda: WideOrder.objects.filter(customer__city="city42").values_list("reference", flat=True),
        expect_rewrite=True,
    )
    assert matched == ["REF-organic", "REF-vendor"]


def test_the_rewritten_sql_is_the_array_form(graph):
    clause = sql_of(WideOrder.objects.filter(customer__city="city42"))
    assert "= ANY (ARRAY(SELECT" in clause
    assert "INNER JOIN" not in clause


@pytest.mark.parametrize(
    "path,value",
    [
        ("customer__city", "city42"),
        ("customer__city__icontains", "CITY42"),
        ("customer__city__startswith", "city4"),
        ("customer__score__gt", 5),
        ("customer__score__isnull", True),
        ("customer__score__in", [10, 30]),
        ("customer__age__range", (40, 41)),
        ("customer__status", "active"),
        ("customer__last_name__iexact", "VENDOR"),
        ("customer__registered_on__year", 2020),
    ],
)
def test_lookups_through_the_relation(graph, path, value):
    same(lambda: WideOrder.objects.filter(**{path: value}).values_list("reference", flat=True), expect_rewrite=True)


def test_two_hops(graph):
    matched = same(
        lambda: WideOrderLine.objects.filter(order__customer__city="city42").values_list("note", flat=True),
        expect_rewrite=True,
    )
    assert matched == ["line-organic", "line-vendor"]


def test_two_hops_then_a_plain_table(graph):
    """The last hop is a plain FK, which must stay a join inside the subquery."""
    same(
        lambda: WideOrderLine.objects.filter(order__customer__region__name="region7").values_list("note", flat=True),
        expect_rewrite=True,
    )


def test_q_and(graph):
    same(
        lambda: WideOrder.objects.filter(Q(customer__city="city42") & Q(status="new")).values_list(
            "reference", flat=True
        ),
        expect_rewrite=True,
    )


def test_q_or(graph):
    same(
        lambda: WideOrder.objects.filter(Q(customer__city="city99") | Q(reference="REF-vendor")).values_list(
            "reference", flat=True
        ),
        expect_rewrite=True,
    )


def test_chained_filters_on_the_same_relation(graph):
    same(
        lambda: (
            WideOrder.objects.filter(customer__city="city42")
            .filter(customer__status="active")
            .values_list("reference", flat=True)
        ),
        expect_rewrite=True,
    )


def test_with_order_by_and_slicing(graph):
    same(
        lambda: (
            WideOrder.objects.filter(customer__city="city42")
            .order_by("reference")[:1]
            .values_list("reference", flat=True)
        ),
        expect_rewrite=True,
    )


def test_count_exists_and_aggregate(graph):
    same(lambda: WideOrder.objects.filter(customer__city="city42").count(), expect_rewrite=True)
    same(lambda: WideOrder.objects.filter(customer__city="city42").exists(), expect_rewrite=True)
    same(lambda: WideOrder.objects.filter(customer__city="city42").aggregate(n=Count("id")), expect_rewrite=True)


def test_values_across_the_relation(graph):
    """Selecting from the far side still needs its own join; the filter is
    still rewritten, and the answer must be unchanged."""
    same(
        lambda: WideOrder.objects.filter(customer__city="city42").values_list("customer__last_name", flat=True),
        expect_rewrite=True,
    )


def test_annotate_and_distinct(graph):
    same(
        lambda: WideCustomer.objects.filter(city="city42").annotate(n=Count("orders")).values_list("last_name", "n"),
        expect_rewrite=False,
    )
    same(
        lambda: WideOrder.objects.filter(customer__city="city42").distinct().values_list("reference", flat=True),
        expect_rewrite=True,
    )


def test_a_null_foreign_key_is_excluded_either_way():
    """The case a semi-join could plausibly get wrong: `col = ANY (ARRAY(...))`
    is NULL when col is NULL, and an inner join drops the row. Both exclude it,
    but it has to be measured rather than assumed."""
    from tests.testapp.models import NullableFkOverlay

    person = Person.objects.create(first_name="Named", age=1)
    NullableFkOverlay.objects.create(label="has-person", person=person)
    NullableFkOverlay.objects.create(label="no-person", person=None)

    matched = same(
        lambda: NullableFkOverlay.objects.filter(person__first_name="Named").values_list("label", flat=True),
        expect_rewrite=True,
    )
    assert matched == ["has-person"]


def test_a_null_foreign_key_and_a_negated_filter():
    """exclude() is not rewritten, but the two must still agree about the NULL
    row — this is the asymmetry that made exclude() unsafe to rewrite."""
    from tests.testapp.models import NullableFkOverlay

    person = Person.objects.create(first_name="Named", age=1)
    NullableFkOverlay.objects.create(label="has-person", person=person)
    NullableFkOverlay.objects.create(label="no-person", person=None)

    excluded = same(
        lambda: NullableFkOverlay.objects.exclude(person__first_name="Named").values_list("label", flat=True),
        expect_rewrite=False,
    )
    # Django *keeps* the null row: exclude() across a relation is built so that
    # "has no related object" counts as "does not match".
    assert excluded == ["no-person"]


def test_the_exclude_guard_is_conservative_not_proven_necessary():
    """Honest record of what was measured, because the guard's stated reason
    turned out to be wrong.

    `_traversal_rewrite()` refuses a negated branch on the grounds that
    `NOT (col = ANY (ARRAY(...)))` and Django's anti-join would disagree about
    a NULL foreign key. They do not, because Django adds the NULL guard itself
    when it negates:

        NOT ("person_id" = ANY (ARRAY(...)) AND "person_id" IS NOT NULL)

    so the null row survives either way. The guard is therefore *conservative*
    rather than necessary, and rewriting exclude() may well be safe. It stays
    refused until it has a matrix of its own — being slow is recoverable and
    being silently wrong is not."""
    from tests.testapp.models import NullableFkOverlay

    person = Person.objects.create(first_name="Named", age=1)
    NullableFkOverlay.objects.create(label="has-person", person=person)
    NullableFkOverlay.objects.create(label="no-person", person=None)

    django_semantics = set(
        NullableFkOverlay.objects.exclude(person__first_name="Named").values_list("label", flat=True)
    )
    inner = Person.objects.filter(first_name="Named").values("pk")
    hand_rewritten = set(NullableFkOverlay.objects.exclude(person_id__in=inner).values_list("label", flat=True))

    assert django_semantics == {"no-person"}
    assert hand_rewritten == django_semantics, "measured: they agree, so the refusal is caution not correctness"

    clause = sql_of(NullableFkOverlay.objects.exclude(person_id__in=inner))
    assert "IS NOT NULL" in clause, "and this is why they agree"


def test_nullable_relation_isnull_both_ways():
    from tests.testapp.models import NullableFkOverlay

    person = Person.objects.create(first_name="Named", age=1)
    NullableFkOverlay.objects.create(label="has-person", person=person)
    NullableFkOverlay.objects.create(label="no-person", person=None)

    same(
        lambda: NullableFkOverlay.objects.filter(person__isnull=True).values_list("label", flat=True),
        expect_rewrite=False,
    )
    same(
        lambda: NullableFkOverlay.objects.filter(person__age__gte=0).values_list("label", flat=True),
        expect_rewrite=True,
    )


def test_a_match_on_nothing(graph):
    matched = same(
        lambda: WideOrder.objects.filter(customer__city="nowhere").values_list("reference", flat=True),
        expect_rewrite=True,
    )
    assert matched == []


def test_prefetch_related_alongside(graph):
    same(
        lambda: [c.last_name for c in WideCustomer.objects.prefetch_related("orders").filter(city="city42")],
        expect_rewrite=False,
    )


def test_update_through_a_traversal_filter(graph):
    WideOrder.objects.filter(customer__city="city42").update(status="paid")
    assert sorted(WideOrder.objects.filter(status="paid").values_list("reference", flat=True)) == [
        "REF-organic",
        "REF-vendor",
    ]


def test_delete_through_a_traversal_filter(graph):
    WideOrderLine.objects.all().delete()
    WideOrder.objects.filter(customer__city="city99").delete()
    assert sorted(WideOrder.objects.values_list("reference", flat=True)) == ["REF-organic", "REF-vendor"]


def test_a_uuid_strategy_rewrites_too(graph):
    """The rewrite must not depend on the id being an integer."""
    from tests.testapp.models import WideCustomerU7, WideOrderU7
    from tests.testapp_shared.models import WideCustomerU7Source

    source = WideCustomerU7Source.objects.create(
        first_name="v",
        last_name="v",
        email="v@x",
        age=1,
        city="city42",
        postcode="p",
        status="active",
        score=1,
        registered_on="2020-01-01",
        notes="",
    )
    customer = WideCustomerU7.objects.get(pk=source.id)
    WideOrderU7.objects.create(
        reference="U7",
        status="new",
        total_cents=1,
        placed_on="2021-01-01",
        channel="web",
        currency="GBP",
        customer=customer,
    )
    matched = same(
        lambda: WideOrderU7.objects.filter(customer__city="city42").values_list("reference", flat=True),
        expect_rewrite=True,
    )
    assert matched == ["U7"]


# ------------------------------------------------------ it must NOT rewrite


def test_exclude_is_left_alone(graph):
    """`NOT (col = ANY(ARRAY(…)))` and Django's anti-join differ on a NULL
    foreign key, so exclude() is refused outright."""
    same(
        lambda: WideOrder.objects.exclude(customer__city="city42").values_list("reference", flat=True),
        expect_rewrite=False,
    )


def test_negated_q_is_left_alone(graph):
    same(
        lambda: WideOrder.objects.filter(~Q(customer__city="city42")).values_list("reference", flat=True),
        expect_rewrite=False,
    )


def test_exclude_still_returns_the_complement(graph):
    included = set(WideOrder.objects.filter(customer__city="city42").values_list("reference", flat=True))
    excluded = set(WideOrder.objects.exclude(customer__city="city42").values_list("reference", flat=True))
    assert included == {"REF-vendor", "REF-organic"}
    assert excluded == {"REF-elsewhere"}


def test_a_reverse_traversal_is_left_alone(graph):
    """One customer has many orders, so the join multiplies rows and a
    semi-join would silently deduplicate."""
    same(
        lambda: WideCustomer.objects.filter(orders__status="new").values_list("last_name", flat=True),
        expect_rewrite=False,
    )


def test_a_reverse_traversal_still_multiplies_rows(graph):
    """Pinning the behaviour the guard protects: two orders for one customer
    must yield that customer twice without distinct()."""
    WideOrder.objects.create(
        reference="REF-second",
        status="new",
        total_cents=1,
        placed_on="2021-01-01",
        channel="web",
        currency="GBP",
        customer=graph["vendor"],
    )
    names = list(WideCustomer.objects.filter(orders__status="new").values_list("last_name", flat=True))
    assert names.count("vendor") == 2


def test_a_many_to_many_traversal_is_fenced_not_replaced():
    """The join stays; a redundant conjunct is added beside it.

    Replacing the join with a semi-join would be wrong here — see
    OverlayQuery._m2m_fence() — so what changes is the SQL, never the rows.
    `same()` asserts the rows are identical either way."""
    roster_source = RosterSource.objects.create(title="r")
    member_source = MemberSource.objects.create(name="m")
    roster = Roster.objects.get(pk=-roster_source.id)
    roster.members.add(Member.objects.get(pk=-member_source.id))

    same(lambda: Roster.objects.filter(members__name="m").values_list("title", flat=True), expect_rewrite=True)

    sql = str(Roster.objects.filter(members__name="m").query)
    assert "INNER JOIN" in sql, "the join must survive, or multiplicity changes"
    assert "= ANY (ARRAY" in sql


def test_the_fence_lookup_is_registered_on_nothing():
    """The fence's private lookup must stay unreachable from application code.

    It used to be `models.Field.register_lookup(OverlayFencedIn)`, which put the
    name on every field of every model in any project that imported this
    package — including plain non-overlay ones — to serve a single internal
    call site. `OverlayQuery.build_lookup()` constructs it directly instead.

    Not merely a tidiness rule. The array form is only faster while the
    subquery is small: measured on a twenty-aggregate summary at 1,000,000 view
    rows it was 2.0x faster than a plain `IN` at a 25,000-row scope and 2.3x
    *slower* at a 1,000,000-row one. Nothing at compile time can tell which
    side of that a given query falls on, so it must not be reachable by anyone
    who has not established, as `_m2m_fence()` has, that the conjunct is
    redundant with a join already in the query.
    """
    name = OverlayFencedIn.lookup_name
    assert models.Field.get_lookups().get(name) is None, "must not be on every Django field"
    assert Person._meta.pk.get_lookups().get(name) is None, "not on overlay pks either"

    # Unreachable through a plain Query, which is what a non-overlay model has.
    with pytest.raises(FieldError):
        PlainPerson.objects.filter(pk__overlay_fenced_in=PlainPerson.objects.values("pk")).query.sql_with_params()

    # Still reachable through OverlayQuery, or the fence itself would be dead.
    statement = str(Roster.objects.filter(members__name="m").query)
    assert "= ANY (ARRAY" in statement, "the fence must still compile"


def test_the_m2m_fence_preserves_row_multiplicity():
    """The property the whole design rests on.

    A roster with three matching members must appear three times, exactly as
    Django's join produces. A semi-join would collapse it to one — measured on
    the production graph as 6 rows where the join returned 10."""
    roster_source = RosterSource.objects.create(title="r")
    roster = Roster.objects.get(pk=-roster_source.id)
    for _ in range(3):
        member_source = MemberSource.objects.create(name="shared")
        roster.members.add(Member.objects.get(pk=-member_source.id))

    with OFF:
        plain = list(Roster.objects.filter(members__name="shared").values_list("title", flat=True))
    fenced = list(Roster.objects.filter(members__name="shared").values_list("title", flat=True))

    assert len(plain) == 3, "the unfenced join should multiply rows"
    assert fenced == plain


def test_an_excluded_m2m_traversal_is_left_alone():
    """Under negation the conjunct stops being implied: NOT (A AND B) is not
    NOT A. The fence is only sound when ANDed with what implies it."""
    roster_source = RosterSource.objects.create(title="r")
    member_source = MemberSource.objects.create(name="m")
    roster = Roster.objects.get(pk=-roster_source.id)
    roster.members.add(Member.objects.get(pk=-member_source.id))
    RosterSource.objects.create(title="untouched")

    same(
        lambda: Roster.objects.exclude(members__name="m").values_list("title", flat=True),
        expect_rewrite=False,
    )


def test_isnull_on_the_m2m_relation_is_left_alone():
    """`members__isnull` asks about the join's existence, not about a row on
    the far side, so there is no set of ids to fence against."""
    roster_source = RosterSource.objects.create(title="r")
    member_source = MemberSource.objects.create(name="m")
    roster = Roster.objects.get(pk=-roster_source.id)
    roster.members.add(Member.objects.get(pk=-member_source.id))
    RosterSource.objects.create(title="empty")

    same(lambda: Roster.objects.filter(members__isnull=True).values_list("title", flat=True), expect_rewrite=False)


def test_in_and_pk_on_the_m2m_relation_are_fenced():
    """Neither is written by hand much. Both are what a related manager and
    `prefetch_related()` emit, and both still join through the link table, so
    both are worth fencing — `person.addresses.all()` measured 32x a plain
    table until they were."""
    roster_source = RosterSource.objects.create(title="r")
    member_source = MemberSource.objects.create(name="m")
    roster = Roster.objects.get(pk=-roster_source.id)
    member = Member.objects.get(pk=-member_source.id)
    roster.members.add(member)

    same(lambda: Roster.objects.filter(members__in=[member]).values_list("title", flat=True), expect_rewrite=True)
    same(lambda: Roster.objects.filter(members__pk=member.pk).values_list("title", flat=True), expect_rewrite=True)


def test_the_related_manager_is_fenced():
    """The detail page. `roster.members.all()` never calls filter() itself —
    the manager builds `Member.objects.filter(rosters__id=<pk>)`, a *reverse*
    m2m path, which the forward-only fence never saw."""
    roster_source = RosterSource.objects.create(title="r")
    member_source = MemberSource.objects.create(name="m")
    roster = Roster.objects.get(pk=-roster_source.id)
    roster.members.add(Member.objects.get(pk=-member_source.id))

    same(lambda: roster.members.all().values_list("name", flat=True), expect_rewrite=True)
    assert "= ANY (ARRAY" in str(roster.members.all().query)
    assert "INNER JOIN" in str(roster.members.all().query), "the join must survive"


def test_prefetch_related_is_fenced():
    rosters = []
    for index in range(3):
        roster_source = RosterSource.objects.create(title=f"r{index}")
        member_source = MemberSource.objects.create(name=f"m{index}")
        roster = Roster.objects.get(pk=-roster_source.id)
        roster.members.add(Member.objects.get(pk=-member_source.id))
        rosters.append(roster)

    # By title, not by id: under NEGATIVE_ID the ids descend as rows are
    # created, so ordering by id would read backwards and say nothing.
    with OFF:
        plain = [[m.name for m in r.members.all()]
                 for r in Roster.objects.prefetch_related("members").order_by("title")]
    fenced = [[m.name for m in r.members.all()]
              for r in Roster.objects.prefetch_related("members").order_by("title")]

    assert fenced == plain
    assert plain == [["m0"], ["m1"], ["m2"]]


def test_the_reverse_fence_preserves_multiplicity():
    """Same property as the forward direction, checked from the other side."""
    member_source = MemberSource.objects.create(name="shared")
    member = Member.objects.get(pk=-member_source.id)
    for index in range(3):
        roster_source = RosterSource.objects.create(title=f"r{index}")
        Roster.objects.get(pk=-roster_source.id).members.add(member)

    with OFF:
        plain = list(Member.objects.filter(rosters__title__startswith="r").values_list("name", flat=True))
    fenced = list(Member.objects.filter(rosters__title__startswith="r").values_list("name", flat=True))

    assert len(plain) == 3, "one row per matching roster"
    assert fenced == plain


def test_a_plain_foreign_key_is_left_alone(graph):
    same(
        lambda: WideCustomer.objects.filter(region__name="region7").values_list("last_name", flat=True),
        expect_rewrite=False,
    )


@pytest.mark.parametrize("path,value", [("customer__isnull", True), ("customer__isnull", False)])
def test_isnull_on_the_relation_is_left_alone(graph, path, value):
    same(lambda: WideOrder.objects.filter(**{path: value}).values_list("reference", flat=True), expect_rewrite=False)


def test_pk_paths_are_left_alone(graph):
    """Django trims these to a local column and never joins at all."""
    pk = graph["vendor"].pk
    for path in ("customer__pk", "customer__id"):
        same(
            lambda p=path: WideOrder.objects.filter(**{p: pk}).values_list("reference", flat=True), expect_rewrite=False
        )


def test_in_on_the_relation_is_left_alone(graph):
    same(
        lambda: WideOrder.objects.filter(customer__in=[graph["vendor"]]).values_list("reference", flat=True),
        expect_rewrite=False,
    )


def test_an_expression_value_is_left_alone(graph):
    """F() would be resolved against the inner model instead of this one."""
    same(
        lambda: WideOrder.objects.filter(customer__score=F("total_cents")).values_list("reference", flat=True),
        expect_rewrite=False,
    )


def test_an_outerref_is_left_alone(graph):
    inner = WideOrder.objects.filter(customer__score=OuterRef("score")).values("reference")[:1]
    same(lambda: WideCustomer.objects.annotate(r=Subquery(inner)).values_list("last_name", "r"), expect_rewrite=False)


def test_a_non_traversal_filter_is_left_alone(graph):
    same(lambda: WideOrder.objects.filter(status="new").values_list("reference", flat=True), expect_rewrite=False)
    same(
        lambda: WideOrder.objects.filter(reference__startswith="REF").values_list("reference", flat=True),
        expect_rewrite=False,
    )


# ------------------------------------------------------------- the setting


def test_it_can_be_turned_off(graph):
    with OFF:
        assert "INNER JOIN" in sql_of(WideOrder.objects.filter(customer__city="city42"))
    assert "= ANY (ARRAY(SELECT" in sql_of(WideOrder.objects.filter(customer__city="city42"))


@override_settings(DJANGO_OVERLAY_REWRITE_TRAVERSALS="please")
def test_a_non_boolean_setting_is_refused():
    with pytest.raises(ImproperlyConfigured, match="must be a bool"):
        sql_of(WideOrder.objects.filter(customer__city="city42"))


def test_it_is_on_by_default():
    from django_overlay.models import _rewrite_traversals_enabled

    assert _rewrite_traversals_enabled() is True


def test_the_queryset_installs_the_rewriting_query():
    from django_overlay.models import OverlayQuery

    assert isinstance(WideOrder.objects.all().query, OverlayQuery)
    assert isinstance(WideOrder.objects.filter(status="new").query, OverlayQuery)


def test_a_supplied_query_is_respected():
    """`QuerySet(model, query=...)` must keep the query it was handed —
    prefetch and combinator paths rely on it."""
    from django_overlay.models import OverlayQuery, OverlayQuerySet

    handed = OverlayQuery(WideOrder)
    assert OverlayQuerySet(model=WideOrder, query=handed).query is handed


def test_an_overlay_fk_pointing_at_a_plain_model_is_left_alone():
    """`OverlayForeignKey` normally targets an overlay model, but the guard
    does not assume it: view -> plain measures 1.2-1.3x because the plain
    side's statistics rescue the estimate, so there is nothing to fence."""
    from unittest import mock

    from django_overlay.models import OverlayQuery

    field = WideOrder._meta.get_field("customer")
    query = OverlayQuery(WideOrder)

    assert query._traversal_rewrite(("customer__city", "city42")) is not None
    with mock.patch.object(field.remote_field, "model", WideRegion):
        assert query._traversal_rewrite(("customer__name", "region7")) is None


def test_a_reverse_path_never_resolves_to_the_foreign_key():
    """The invariant the forward-only guard leans on: a reverse accessor
    resolves to a ManyToOneRel, not to the OverlayForeignKey, so `isinstance`
    is what keeps multiplying traversals out."""
    from django.db.models.fields.reverse_related import ManyToOneRel

    from django_overlay.fields import OverlayForeignKey

    reverse = WideCustomer._meta.get_field("orders")
    assert isinstance(reverse, ManyToOneRel)
    assert not isinstance(reverse, OverlayForeignKey)

    forward = WideOrder._meta.get_field("customer")
    assert isinstance(forward, OverlayForeignKey)
    assert forward.many_to_one is True


# ------------------------------------------- exhaustive combinatorial sweep


def clause_pool():
    """Filter clauses that between them cover every branch of the rewrite:
    rewritten and not, nullable and not, negated, across a plain table, and
    plain local columns."""
    return {
        "cust_city": Q(customer__city="city42"),
        "cust_city_other": Q(customer__city="city99"),
        "cust_status": Q(customer__status="active"),
        "cust_score_gt": Q(customer__score__gt=5),
        "cust_score_null": Q(customer__score__isnull=True),
        "cust_region": Q(customer__region__name="region7"),
        "cust_pk_path": Q(customer__pk__gt=0),
        "cust_isnull": Q(customer__isnull=False),
        "local_status": Q(status="new"),
        "local_ref": Q(reference__startswith="REF"),
        "negated": ~Q(customer__city="city42"),
    }


def combined(names, connector):
    pool = clause_pool()
    combined_q = pool[names[0]]
    for name in names[1:]:
        combined_q = combined_q & pool[name] if connector == "AND" else combined_q | pool[name]
    return combined_q


@pytest.mark.parametrize("connector", ["AND", "OR"])
@pytest.mark.parametrize(
    "names",
    [pair for pair in __import__("itertools").combinations(sorted(clause_pool()), 2)],
)
def test_every_pair_of_clauses_agrees(graph, names, connector):
    """56 pairs x 2 connectors, each run with the rewrite on and off."""
    same_result_regardless(graph, combined(names, connector))


@pytest.mark.parametrize("connector", ["AND", "OR"])
@pytest.mark.parametrize(
    "names",
    [triple for triple in __import__("itertools").combinations(sorted(clause_pool()), 3)][::7],
)
def test_a_sample_of_triples_agrees(graph, names, connector):
    same_result_regardless(graph, combined(names, connector))


def same_result_regardless(graph, condition):
    """Identical rows with the rewrite on and off, for both filter and exclude,
    and with a slice on top so LIMIT is in play."""
    for build in (
        lambda: WideOrder.objects.filter(condition).values_list("reference", flat=True),
        lambda: WideOrder.objects.exclude(condition).values_list("reference", flat=True),
        lambda: WideOrder.objects.filter(condition).order_by("reference")[:2].values_list("reference", flat=True),
        lambda: WideOrder.objects.filter(condition).count(),
    ):
        with OFF:
            plain, _ = run(build)
        rewritten, _ = run(build)
        assert rewritten == plain, f"{condition} disagreed:\n  off: {plain}\n  on : {rewritten}"


def test_the_queryset_passes_using_and_hints_through():
    """`hints` reaches database routers as `db_for_read(model, **hints)`, and
    related managers populate it with `instance`. Overriding __init__ to
    install OverlayQuery must not drop either argument on the way past.

    Pinned because a mutant that replaced `hints=hints` with `hints=None`
    survived the whole suite — nothing else looked at it."""
    from django_overlay.models import OverlayQuerySet

    marker = object()
    queryset = OverlayQuerySet(model=WideOrder, using="default", hints={"instance": marker})

    assert queryset._hints == {"instance": marker}
    assert queryset._db == "default"


def test_the_queryset_defaults_are_unchanged():
    from django_overlay.models import OverlayQuerySet

    queryset = OverlayQuerySet(model=WideOrder)

    assert queryset._hints == {}
    assert queryset._db is None


def test_the_select_related_setting_is_validated_the_same_way():
    """Every DJANGO_OVERLAY_* boolean refuses a non-boolean rather than
    treating a truthy string as "on" -- `"false"` is truthy, and a setting that
    silently means the opposite of what it says is worse than a crash."""
    from django_overlay.models import _redirect_select_related_enabled

    with override_settings(DJANGO_OVERLAY_REDIRECT_SELECT_RELATED="please"):
        with pytest.raises(ImproperlyConfigured, match="must be a bool"):
            _redirect_select_related_enabled()
    assert _redirect_select_related_enabled() is True


def test_the_fence_declines_when_the_target_is_not_a_view(graph):
    """view -> plain does not need the fence, and must not get it.

    The fence exists because an appendrel parent carries no statistics for the
    planner to size a join with. A plain table has them, so the estimate is
    already sound -- and `_m2m_fence()` checks the *target*, not just the
    through model, for exactly that reason.
    """
    fenced = sql_of(Roster.objects.filter(members__name="m"))
    assert "= ANY (ARRAY(SELECT" in fenced, "the fence must apply normally for this to prove anything"

    with mock.patch.object(Member, "_is_overlay_view_model", False):
        assert "= ANY (ARRAY(SELECT" not in sql_of(Roster.objects.filter(members__name="m"))


def test_the_fence_declines_a_through_model_it_cannot_read(graph):
    """A non-standard through model whose m2m accessors raise.

    Django's `m2m_field_name()` walks the through model's fields to find the
    two ends. A hand-written through with an unusual shape can make that fail,
    and the fence has to decline rather than propagate an exception out of
    query construction, where it would surface as a broken filter() rather
    than as a missing optimisation.
    """
    # Patched on the instance, not the class: Django attaches m2m_field_name
    # per field in contribute_to_class rather than defining it on the type.
    field = Roster._meta.get_field("members")
    with mock.patch.object(
        field, "m2m_field_name", side_effect=RuntimeError("unusual through model")
    ):
        assert "= ANY (ARRAY(SELECT" not in sql_of(Roster.objects.filter(members__name="m"))
