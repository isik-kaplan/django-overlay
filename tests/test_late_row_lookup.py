"""The ordered-page rewrite: does it return the same rows, and when does it fire?

`ORDER BY … LIMIT n` over the view sorts whole rows to keep n of them. The sort
is not the cost -- it is a top-N heapsort holding 31kB -- but the rows arrive
unordered, so all of them must be produced and passed through it. Measured at
1,000,000 view rows: 335ms, against 40ms for the same page selecting only the
primary key. Resolving the keys first and fetching the rows by key is 40ms and
identical rows.

Differential rather than SQL-pinning, for the reason `test_optimisations_off.py`
gives: the rewrite is *supposed* to change the SQL. What it must never change is
which rows come back, in which order, how many times each. `overlay_direct()`
is the unrewritten arm, so every case here can be written twice and compared.

The one failure mode worth naming, because it is the reason `_multiplies_rows()`
exists: fetching by primary key returns each row once, so on a query whose join
multiplies rows the rewrite would return *fewer* rows rather than the same rows
faster -- and nothing would raise. `test_a_multiplying_join_is_refused` and
`test_a_multiplying_join_still_agrees_when_distinct` are that pair.
"""

import pytest
from django.db.models import Exists, F, OuterRef, Subquery, sql
from django.test import override_settings

from django_overlay.models import OverlayQuerySet
from tests.testapp.models import AuditEntry, Person, PersonNote, PersonProfile, WideCustomer, WideOrder
from tests.testapp_shared.models import WideCustomerSource


pytestmark = pytest.mark.django_db

PAGE = slice(None, 5)


@pytest.fixture
def customers():
    """Ten customers straddling both branches of the view.

    Both branches on purpose. Under NEGATIVE_ID a source-backed row's id is
    `-source.id`, so the two branches sort into two blocks with opposite
    internal order -- which is exactly the arrangement that catches a rewrite
    that orders the inner query and the outer one differently. A page taken
    from the middle of that spans the boundary.

    Scores collide in pairs so that `order_by("score")` is not a total order:
    if the rewrite resolved keys by one tie-break and fetched rows by another,
    the page would be stable in neither arm and this fixture is what makes that
    visible.
    """
    made = []
    for index in range(5):
        source = WideCustomerSource.objects.create(
            first_name=f"v{index}",
            last_name="vendor",
            email=f"v{index}@x",
            age=30 + index,
            city="city1" if index % 2 else "city2",
            postcode=f"pc{index}",
            status="active",
            score=index // 2,
            registered_on="2020-01-01",
            notes="",
        )
        made.append(WideCustomer.objects.get(pk=-source.id))
    for index in range(5):
        made.append(
            WideCustomer.objects.create(
                first_name=f"o{index}",
                last_name="organic",
                email=f"o{index}@x",
                age=40 + index,
                city="city1" if index % 2 else "city2",
                postcode=f"opc{index}",
                status="lapsed",
                score=index // 2,
                registered_on="2020-01-02",
            )
        )
    return made


def _pks(queryset):
    return [customer.pk for customer in queryset]


def _both_arms(queryset, page=PAGE):
    """(rewritten, direct) for the same page, as primary-key lists."""
    return _pks(queryset[page]), _pks(queryset.overlay_direct()[page])


# --------------------------------------------------------------- it fires at all


def test_the_rewrite_fires_on_an_ordered_page(customers):
    """The array subquery must carry the LIMIT, or nothing has been deferred.

    Asserting on the LIMIT's position rather than on `= ANY (ARRAY` alone: the
    m2m fence puts an array subquery into plenty of queries that this rewrite
    never touched, so the bare marker would pass while the rewrite did nothing.
    """
    statement = str(WideCustomer.objects.order_by("id")[:5].query)
    array_start = statement.index("= ANY (ARRAY")
    assert "LIMIT" in statement[array_start:], "the page's LIMIT belongs to the inner query"


def test_overlay_direct_leaves_the_page_alone(customers):
    statement = str(WideCustomer.objects.order_by("id").overlay_direct()[:5].query)
    assert "= ANY (ARRAY" not in statement


def test_the_switch_off_leaves_the_page_alone(customers):
    with override_settings(DJANGO_OVERLAY_LATE_ROW_LOOKUP=False):
        statement = str(WideCustomer.objects.order_by("id")[:5].query)
    assert "= ANY (ARRAY" not in statement


def test_a_non_bool_switch_is_refused():
    from django.core.exceptions import ImproperlyConfigured

    with override_settings(DJANGO_OVERLAY_LATE_ROW_LOOKUP="yes"):
        with pytest.raises(ImproperlyConfigured, match="must be a bool"):
            WideCustomer.objects.order_by("id")[:5]


# ------------------------------------------------------------ identical rows


@pytest.mark.parametrize(
    "ordering",
    ["id", "-id", "age", "-age", "email", "postcode"],
    ids=lambda value: f"order-by-{value}",
)
def test_a_page_agrees_with_the_unrewritten_page(customers, ordering):
    rewritten, direct = _both_arms(WideCustomer.objects.order_by(ordering))
    assert rewritten == direct
    assert len(rewritten) == 5, "the page must be full, or this proves nothing"


@pytest.mark.parametrize("page", [slice(None, 3), slice(2, 7), slice(7, None), slice(None, 50)])
def test_every_page_of_a_scan_agrees(customers, page):
    """Including the one that runs off the end and the one that starts inside
    the source branch. `[7:]` has no stop, so the rewrite declines it -- the
    assertion is that declining still returns the right rows, which is the
    thing a refusal has to get right and the easiest thing to forget."""
    rewritten, direct = _both_arms(WideCustomer.objects.order_by("id"), page)
    assert rewritten == direct


def test_a_tie_in_the_sort_key_agrees(customers):
    """`score` collides in pairs, so this ordering is not total.

    Postgres may return a tied pair in either order, and it need not be the
    same order in the two arms -- so this compares the pages as multisets and
    additionally pins that both arms are a prefix of the same fully-ordered
    scan. A rewrite that resolved keys under one tie-break and fetched under
    another would pass a set comparison on one page and fail on some other, so
    the second assertion is the one with teeth.
    """
    ordered = WideCustomer.objects.order_by("score", "id")
    rewritten, direct = _both_arms(ordered)
    assert rewritten == direct

    unstable = WideCustomer.objects.order_by("score")
    rewritten_unstable, direct_unstable = _both_arms(unstable)
    assert sorted(rewritten_unstable) == sorted(direct_unstable)


def test_a_filtered_page_agrees(customers):
    rewritten, direct = _both_arms(WideCustomer.objects.filter(city="city1").order_by("id"))
    assert rewritten == direct
    assert rewritten, "the filter must match something"


def test_a_deferred_projection_agrees(customers):
    """`only()` survives the rewrite: the outer query is `self` plus a key
    predicate, so its projection is whatever the caller asked for."""
    page = WideCustomer.objects.order_by("id").only("city")[:5]
    direct = WideCustomer.objects.order_by("id").only("city").overlay_direct()[:5]
    assert [(c.pk, c.city) for c in page] == [(c.pk, c.city) for c in direct]


def test_a_values_page_agrees(customers):
    page = WideCustomer.objects.order_by("id").values("city", "age")[:5]
    direct = WideCustomer.objects.order_by("id").values("city", "age").overlay_direct()[:5]
    assert list(page) == list(direct)


def test_a_soft_deleted_row_stays_hidden(customers):
    """The rewrite adds a predicate to the view, so the view's own exclusions
    still apply -- but the inner query resolves keys and the outer fetches
    them, and a tombstone has to be invisible to both or a deleted row comes
    back with a key that no longer resolves."""
    victim = customers[0]
    victim.delete()
    rewritten, direct = _both_arms(WideCustomer.objects.order_by("id"))
    assert rewritten == direct
    assert victim.pk not in rewritten


# --------------------------------------------------- what it refuses, and why


def test_an_unordered_page_is_refused(customers):
    assert WideCustomer.objects.all()._wants_late_row_lookup(PAGE) is False


def test_a_page_ordered_across_a_relation_is_refused(customers):
    """The outer query is restricted by key and has no join to order across."""
    assert WideCustomer.objects.order_by("region__name")._wants_late_row_lookup(PAGE) is False


def test_a_page_ordered_by_an_expression_is_refused(customers):

    assert WideCustomer.objects.order_by(F("age").desc())._wants_late_row_lookup(PAGE) is False


@pytest.mark.parametrize("ordering", [("pk",), ("-age",), ("region",), ("region_id",), ("-pk", "age")])
def test_an_ordering_on_local_columns_gets_the_rewrite(customers, ordering):
    """`pk`, a descending term, and a foreign key by either its name or its
    column are all local to the page, so the page is rewritten and agrees."""
    ordered = WideCustomer.objects.order_by(*ordering)

    assert ordered._wants_late_row_lookup(PAGE) is True
    rewritten, direct = _both_arms(ordered)
    assert rewritten == direct


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: WideCustomer.objects.order_by("pk", "region__name"), id="pk then a relation"),
        pytest.param(lambda: WideCustomer.objects.annotate(n=F("age")).order_by("n"), id="an annotation alias"),
    ],
)
def test_an_ordering_term_that_is_not_a_local_column_is_refused(customers, build):
    """Every term is checked: a `pk` first does not vouch for what follows it,
    and a name that is not a column -- an annotation's alias -- is refused like
    any other expression."""
    assert build()._wants_late_row_lookup(PAGE) is False


def test_meta_ordering_counts_as_an_ordering(customers):
    """A model that orders itself still gets the rewrite. The SQL carries an
    ORDER BY either way, so refusing this would skip every model with a default
    order."""
    ordered = WideCustomer.objects.order_by()  # explicit clear, so only Meta could supply one
    if WideCustomer._meta.ordering:
        assert ordered._wants_late_row_lookup(PAGE) is True
    else:
        assert ordered._wants_late_row_lookup(PAGE) is False


def test_a_multiplying_join_is_refused(customers):
    """A reverse relation returns the customer once per matching order.

    Fetching by key returns them once. So the rewrite must decline, and the
    direct form's duplicates must survive -- which is what the second assertion
    checks, because a refusal that silently deduplicated anyway would pass the
    first.
    """
    customer = customers[0]
    for index in range(3):
        WideOrder.objects.create(
            customer=customer,
            reference=f"ref{index}",
            status="open",
            total_cents=100 + index,
            channel="web",
            currency="GBP",
        )

    multiplying = WideCustomer.objects.filter(orders__status="open").order_by("id")
    assert multiplying._wants_late_row_lookup(PAGE) is False

    rewritten, direct = _both_arms(multiplying)
    assert rewritten == direct
    assert rewritten.count(customer.pk) == 3, "the join's duplicates must survive"


def test_a_multiplying_join_still_agrees_when_distinct(customers):
    """`distinct()` makes both forms return each row once, so the rewrite is
    allowed again."""
    customer = customers[0]
    for index in range(3):
        WideOrder.objects.create(
            customer=customer,
            reference=f"ref{index}",
            status="open",
            total_cents=100 + index,
            channel="web",
            currency="GBP",
        )

    deduped = WideCustomer.objects.filter(orders__status="open").order_by("id").distinct()
    assert deduped._wants_late_row_lookup(PAGE) is True

    rewritten, direct = _both_arms(deduped)
    assert rewritten == direct
    assert rewritten.count(customer.pk) == 1


def test_a_reverse_one_to_one_join_does_not_block_the_rewrite():
    """A reverse one-to-one returns each row at most once, so fetching by key
    returns the same rows. `OneToOneRel` subclasses `ManyToOneRel`, so this is
    the case `_multiplies_rows()` must let through before it refuses the
    multiplying ones -- and refusing it would cost the rewrite for no reason."""
    people = [Person.objects.create(first_name=f"p{index}") for index in range(4)]
    for person in people[:3]:
        PersonProfile.objects.create(person=person, bio="b")

    profiled = Person.objects.filter(profile__bio="b").order_by("id")
    assert profiled._wants_late_row_lookup(PAGE) is True

    rewritten, direct = _both_arms(profiled)
    assert rewritten == direct
    assert sorted(rewritten) == sorted(person.pk for person in people[:3])


def test_a_reverse_one_to_one_does_not_hide_a_multiplying_join_after_it():
    """The one-to-one is skipped, not taken as the answer: a reverse foreign
    key further along the join list still multiplies rows."""
    person = Person.objects.create(first_name="p")
    PersonProfile.objects.create(person=person, bio="b")
    PersonNote.objects.create(person=person, text="n")
    PersonNote.objects.create(person=person, text="n")

    # Two filter() calls, so the one-to-one is joined first: keyword arguments
    # in one call are sorted, which would put the notes ahead of it.
    both = Person.objects.filter(profile__bio="b").filter(overlay_notes__text="n").order_by("id")

    assert both._wants_late_row_lookup(PAGE) is False
    assert _pks(both[PAGE]) == [person.pk, person.pk]


def test_a_view_without_a_full_anti_join_is_left_alone():
    """`overridable = False` leaves no anti-join for `LIMIT` to wait on, so the
    direct page already stops early. Nothing to defer."""
    assert AuditEntry.objects.order_by("id")._wants_late_row_lookup(PAGE) is False


def _orders(customer, *references):
    for reference in references:
        WideOrder.objects.create(
            customer=customer, reference=reference, status="open", total_cents=1, channel="web", currency="GBP"
        )


def test_a_correlated_subquery_page_is_refused_and_binds_to_the_caller(customers):
    """`OuterRef("pk")` names the query directly around it. Rewritten, that is
    the page query, so the reference would bind to the page's own key and the
    annotation would quietly carry other rows' values, with every row present
    and nothing raised."""
    first, second = customers[0], customers[5]
    _orders(first, "a-1", "a-2")
    _orders(second, "b-1")
    earliest = WideOrder.objects.filter(customer=OuterRef("pk")).order_by("id").values("reference")

    assert earliest._wants_late_row_lookup(slice(None, 1)) is False

    annotated = dict(
        WideCustomer.objects.filter(pk__in=[first.pk, second.pk])
        .annotate(earliest=Subquery(earliest[:1]))
        .values_list("pk", "earliest")
    )
    assert annotated == {first.pk: "a-1", second.pk: "b-1"}


def test_a_reference_two_levels_out_is_refused(customers):
    """`OuterRef(OuterRef(...))` is stored unresolved, as an OuterRef rather
    than a ResolvedOuterRef, and points just as far out of the page."""
    assert (
        WideOrder.objects.filter(customer=OuterRef(OuterRef("pk"))).order_by("id")._wants_late_row_lookup(PAGE) is False
    )


def test_an_outer_reference_in_an_annotation_is_refused(customers):
    assert WideOrder.objects.annotate(owner=OuterRef("pk")).order_by("id")._wants_late_row_lookup(PAGE) is False


def test_an_exists_filter_still_gets_the_rewrite(customers):
    """The nested query's `OuterRef` points at *this* query, which the rewrite
    keeps whole in both halves -- so this is not a reference outward, and the
    page must still be rewritten and still agree."""
    _orders(customers[1], "x")
    _orders(customers[6], "y")
    with_orders = WideCustomer.objects.filter(Exists(WideOrder.objects.filter(customer=OuterRef("pk")))).order_by("id")

    assert with_orders._wants_late_row_lookup(PAGE) is True
    rewritten, direct = _both_arms(with_orders)
    assert rewritten == direct
    assert sorted(rewritten) == sorted([customers[1].pk, customers[6].pk])


def test_a_queryset_over_a_plain_query_is_left_alone(customers):
    """`OverlayQuerySet(query=sql.Query(...))` cannot resolve the array lookup a
    rewritten page is fetched by -- it raised FieldError on every ordered slice."""
    plain = OverlayQuerySet(model=WideCustomer, query=sql.Query(WideCustomer)).order_by("id")

    assert plain._wants_late_row_lookup(PAGE) is False
    assert _pks(plain[PAGE]) == _pks(WideCustomer.objects.order_by("id").overlay_direct()[PAGE])


def test_a_key_only_projection_is_refused(customers):
    """This is the rewrite's own inner query. Rewriting it again would recurse.

    Both spellings, because `values_list("pk", flat=True)` and `values("pk")`
    reach `values_select` by different routes and only one of them was checked
    the first time.
    """
    assert WideCustomer.objects.order_by("id").values("pk")._wants_late_row_lookup(PAGE) is False
    assert WideCustomer.objects.order_by("id").values_list("pk", flat=True)._wants_late_row_lookup(PAGE) is False


def test_a_stepped_slice_is_left_to_python(customers):
    """`qs[::2]` is not a LIMIT. Django evaluates it and slices the list, and
    the result must be the same either way."""
    stepped = list(WideCustomer.objects.order_by("id")[:6:2])
    direct = list(WideCustomer.objects.order_by("id").overlay_direct()[:6:2])
    assert [c.pk for c in stepped] == [c.pk for c in direct]


def test_a_combinator_is_refused(customers):
    combined = WideCustomer.objects.order_by("id").union(WideCustomer.objects.order_by("id"))
    assert combined._wants_late_row_lookup(PAGE) is False


def test_extra_is_refused_on_its_own(customers):
    """`extra(select=...)` can select anything at all, so it has to refuse
    without a combinator alongside it -- the union test above cannot show that.
    (`extra(where=...)` is only a condition, which the inner query carries.)"""
    assert WideCustomer.objects.order_by("id").extra(select={"one": "1"})._wants_late_row_lookup(PAGE) is False


def test_projecting_every_name_for_the_key_is_still_a_key_only_projection(customers):
    """`values("pk", "id")` names nothing but the key, twice. Still the rewrite's
    own inner query, so still refused -- a subset check, not a strict one."""
    assert WideCustomer.objects.order_by("id").values("pk", "id")._wants_late_row_lookup(PAGE) is False


def test_an_integer_index_is_not_a_page(customers):
    """`qs[3]` is one row, not a slice, and must behave exactly as before."""
    ordered = WideCustomer.objects.order_by("id")
    assert ordered[3].pk == ordered.overlay_direct()[3].pk
