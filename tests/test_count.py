"""`count()` decomposed into two counts.

Postgres won't push an aggregate through `UNION ALL`, so `count(*)` on the view
materialises the whole Append. Counting each branch separately measured
639ms -> 261ms at 3,000,000 rows.

Two things have to hold, and both are tested for every configuration:

  * the answer is identical to `count(*)` on the view — asserted against the
    view directly rather than against a hardcoded number, since that is the
    actual invariant;
  * the fast path is the one that ran. A test that only checks the number
    would still pass if the optimisation silently stopped applying, which is
    the failure mode worth protecting against.
"""

import pytest
from django.db import connection
from django.db.models import Avg, Count, Max, Q
from django.test.utils import CaptureQueriesContext

from tests.testapp.models import (
    Address,
    HardDeleteCountTest,
    Person,
    PersonNote,
    PersonUuid4,
    PersonUuid7Polyfill,
    SoftDeleteTestNoSource,
)
from tests.testapp_shared.models import (
    FilteredSourceTestSource,
    HardDeleteCountTestSource,
    PersonSource,
    PersonSourceUuid4,
    PersonSourceUuid7Polyfill,
)


pytestmark = pytest.mark.django_db


def view_count(model) -> int:
    """`count(*)` straight at the view — the number the decomposition has to
    reproduce."""
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT count(*) FROM "{model._meta.db_table}"')  # noqa: S608 - identifier from _meta
        return cursor.fetchone()[0]


def decomposed(queryset) -> bool:
    """Did the fast path run? The decomposed form issues one statement holding
    two count(*) subqueries and never names the view."""
    with CaptureQueriesContext(connection) as captured:
        queryset.count()
    assert len(captured) == 1, f"expected a single statement, got {len(captured)}"
    sql = captured[0]["sql"]
    return sql.count("count(*)") == 2 and queryset.model._meta.db_table not in sql


@pytest.fixture
def mixed_people():
    """Every kind of row the view can show, so a count that mishandles any one
    of them is wrong: an untouched source row, a materialised override, an
    organic row, and a tombstoned source row.

    Visible: 1 override + 1 organic + 1 untouched source = 3. The tombstoned
    row is hidden on both sides — its base row fails `NOT _overlay_deleted`,
    and its source row is shadowed by the anti-join."""
    untouched, overridden, deleted = (PersonSource.objects.create(first_name=f"s{i}", age=i) for i in range(3))

    override = Person.objects.get(pk=-overridden.id)
    override.age = 99
    override.save()

    Person.objects.create(first_name="organic", age=7)
    Person.objects.get(pk=-deleted.id).delete()

    return {"untouched": untouched, "overridden": overridden, "deleted": deleted}


# ------------------------------------------------- the answer is unchanged


def test_counts_every_kind_of_row(mixed_people):
    assert Person.objects.count() == view_count(Person) == 3


def test_takes_the_decomposed_path(mixed_people):
    assert decomposed(Person.objects.all())


def test_matches_the_view_with_no_rows_at_all():
    """Both subqueries return 0 — worth pinning separately, because a sum of
    two empty counts is where a NULL would show up if either side were
    written as an aggregate over an empty set without care."""
    assert Person.objects.count() == view_count(Person) == 0


def test_matches_the_view_with_only_source_rows():
    PersonSource.objects.create(first_name="only", age=1)
    assert Person.objects.count() == view_count(Person) == 1


def test_matches_the_view_with_only_organic_rows():
    Person.objects.create(first_name="only", age=1)
    assert Person.objects.count() == view_count(Person) == 1


def test_an_override_is_counted_once_not_twice(mixed_people):
    """The anti-join is what stops double counting, and the decomposition has
    to reproduce it rather than add the two tables together."""
    assert PersonSource.objects.count() == 3, "three source rows exist"
    assert Person.objects.count() == 3, "but one is shadowed and one tombstoned"


# --------------------------------------------------- across configurations


def test_uuid4_source_ids_are_not_negated():
    """`negates_source_ids` is False here, so the anti-join compares the id
    directly. Getting that backwards would shadow nothing and overcount."""
    source = PersonSourceUuid4.objects.create(first_name="s", age=1)
    override = PersonUuid4.objects.get(pk=source.id)
    override.age = 2
    override.save()
    PersonUuid4.objects.create(first_name="organic", age=3)

    assert decomposed(PersonUuid4.objects.all())
    assert PersonUuid4.objects.count() == view_count(PersonUuid4) == 2


def test_uuid7_polyfill():
    source = PersonSourceUuid7Polyfill.objects.create(first_name="s", age=1)
    override = PersonUuid7Polyfill.objects.get(pk=source.id)
    override.age = 2
    override.save()

    assert decomposed(PersonUuid7Polyfill.objects.all())
    assert PersonUuid7Polyfill.objects.count() == view_count(PersonUuid7Polyfill) == 1


def test_extra_where_on_the_source_is_honoured():
    """FilteredSourceTest's source carries `extra_where="active"`. A count that
    dropped it would include the inactive row the view hides."""
    from tests.testapp.models import FilteredSourceTest

    FilteredSourceTestSource.objects.create(first_name="visible", active=True)
    FilteredSourceTestSource.objects.create(first_name="hidden", active=False)

    assert decomposed(FilteredSourceTest.objects.all())
    assert FilteredSourceTest.objects.count() == view_count(FilteredSourceTest) == 1


def test_hard_delete_model_has_no_deleted_filter():
    """soft_delete = False, so the base branch carries no
    `WHERE NOT _overlay_deleted` — the column doesn't exist to filter on."""
    source = HardDeleteCountTestSource.objects.create(first_name="s")
    HardDeleteCountTest.objects.create(first_name="organic")

    assert decomposed(HardDeleteCountTest.objects.all())
    # The statement itself, not just its answer. What replaces the WHERE clause
    # here is an empty string spliced straight after the table name, and
    # anything non-empty put there parses as a table alias instead -- valid SQL
    # returning the same count, so no assertion on the number can see it.
    with CaptureQueriesContext(connection) as captured:
        HardDeleteCountTest.objects.count()
    assert captured[0]["sql"] == (
        'SELECT (SELECT count(*) FROM "harddeletecounttest")'
        ' + (SELECT count(*) FROM "public"."testapp_shared_harddeletecounttestsource"'
        ' WHERE NOT EXISTS (SELECT 1 FROM "harddeletecounttest" AS overlay_base'
        ' WHERE overlay_base."id" = -"testapp_shared_harddeletecounttestsource"."id"))'
    )
    assert HardDeleteCountTest.objects.count() == view_count(HardDeleteCountTest) == 2

    HardDeleteCountTest.objects.get(pk=-source.id).delete()
    assert HardDeleteCountTest.objects.count() == view_count(HardDeleteCountTest)


# ------------------------------------------- everything else falls through


def test_a_model_without_a_source_falls_through():
    """No source means no second branch to count — the view is just the base
    table, and Django's own count is already the right query."""
    SoftDeleteTestNoSource.objects.create(label="a")
    assert not decomposed(SoftDeleteTestNoSource.objects.all())
    assert SoftDeleteTestNoSource.objects.count() == 1


@pytest.mark.parametrize(
    ("label", "build"),
    [
        ("filtered", lambda: Person.objects.filter(age=99)),
        ("excluded", lambda: Person.objects.exclude(age=99)),
        ("Q filtered", lambda: Person.objects.filter(Q(age=99) | Q(first_name="organic"))),
        ("sliced", lambda: Person.objects.all()[:2]),
        ("annotated", lambda: Person.objects.annotate(n=Count("overlay_notes"))),
        ("distinct", lambda: Person.objects.distinct()),
        ("distinct on", lambda: Person.objects.order_by("age").distinct("age")),
        ("union", lambda: Person.objects.all().union(Person.objects.all())),
        ("extra", lambda: Person.objects.extra(where=["age > 0"])),
        ("joined via values", lambda: Person.objects.values("addresses__city")),
    ],
)
def test_narrowing_the_queryset_falls_through(mixed_people, label, build):
    queryset = build()
    assert not decomposed(queryset), f"{label} must not take the whole-table path"


def test_the_fallthrough_answers_are_still_right(mixed_people):
    """Falling through has to mean Django's ordinary count, not a broken one."""
    assert Person.objects.filter(age=99).count() == 1
    assert Person.objects.all()[:2].count() == 2
    assert Person.objects.distinct().count() == 3


def test_a_join_that_multiplies_rows_is_not_counted_as_the_whole_table(mixed_people):
    """The `alias_map` guard, and why it is not paranoia: the joined count and
    the view count genuinely differ.

    The join is a LEFT OUTER, so the person with two addresses contributes two
    rows and the two address-less people contribute a NULL row each — 4 against
    the view's 3. Taking the whole-table path here would answer 3."""
    person = Person.objects.get(first_name="organic")
    for city in ("here", "there"):
        person.addresses.add(Address.objects.create(street="s", city=city))

    joined = Person.objects.values("addresses__city")
    assert not decomposed(joined)
    assert joined.count() == 4
    assert Person.objects.count() == 3, "which is not what the joined queryset counts"


def test_the_alias_map_guard_sits_between_one_table_and_two(mixed_people):
    """Where exactly the `alias_map` line falls, from both sides.

    `.values('first_name')` puts one alias in the map and is still a count of
    every row, so it has to decompose. One join puts two in, and two is already
    enough to multiply rows -- a reverse FK does it without any m2m through
    table in between, which is the smallest shape that can. Asserting only the
    m2m case (three aliases) left both sides of the comparison free to move.
    """
    person = Person.objects.get(first_name="organic")
    for text in ("first", "second"):
        PersonNote.objects.create(person=person, text=text)

    single = Person.objects.values("first_name")
    joined = Person.objects.values("overlay_notes__id")
    assert len(single.query.alias_map) == 1
    assert len(joined.query.alias_map) == 2

    assert decomposed(single), "values() alone still counts the whole table"
    assert not decomposed(joined)
    assert joined.count() == 4, "two notes on one person, plus a NULL row each for the other two"
    assert Person.objects.count() == 3, "which is not what the joined queryset counts"


def test_a_cached_queryset_counts_its_cache_without_a_query(mixed_people):
    """Django's contract: an evaluated queryset counts what it already has.
    Overriding count() must not turn `len()`-cheap into a round trip."""
    queryset = Person.objects.all()
    list(queryset)

    with CaptureQueriesContext(connection) as captured:
        assert queryset.count() == 3
    assert len(captured) == 0, "the result cache should have answered it"


# ------------------------------- Count() and friends are a different path


def test_aggregate_count_still_works(mixed_people):
    """`aggregate(Count(...))` never reaches QuerySet.count(), so it is
    untouched by the override — and still correct."""
    assert Person.objects.aggregate(n=Count("id"))["n"] == 3
    assert Person.objects.aggregate(n=Count("*"))["n"] == 3


def test_aggregate_count_goes_through_the_view(mixed_people):
    with CaptureQueriesContext(connection) as captured:
        Person.objects.aggregate(n=Count("id"))
    assert Person._meta.db_table in captured[0]["sql"], "aggregate() still queries the view"


def test_other_aggregates_still_work(mixed_people):
    assert Person.objects.aggregate(m=Max("age"))["m"] == 99
    assert Person.objects.aggregate(a=Avg("age"))["a"] is not None


def test_annotated_count_still_works(mixed_people):
    """`annotate(Count(...))` is per-row, not a whole-queryset count."""
    person = Person.objects.get(first_name="organic")
    person.addresses.add(Address.objects.create(street="s", city="c"))

    counts = {p.first_name: p.n for p in Person.objects.annotate(n=Count("addresses"))}
    assert counts["organic"] == 1
    assert all(n == 0 for name, n in counts.items() if name != "organic")


def test_values_annotate_group_by_count_still_works(mixed_people):
    """The `GROUP BY` form — a different query again, and one the guard has to
    leave alone rather than answer with the table total."""
    by_age = dict(Person.objects.values_list("age").annotate(n=Count("id")).order_by())
    assert sum(by_age.values()) == 3


def test_exists_is_unaffected(mixed_people):
    assert Person.objects.exists() is True
    assert Person.objects.filter(age=12345).exists() is False


def test_len_still_matches_count(mixed_people):
    assert len(Person.objects.all()) == Person.objects.count() == 3
