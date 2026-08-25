"""Querying one branch of the view instead of both — TODO/origin.

The view is `base UNION ALL source`, so every row came from exactly one of two
places, and a tenant usually knows which one they mean: records our team has
edited, or vendor records nobody has touched. Until now there was no way to say
so, and no cheap way to work it out — the pk sign says it under NEGATIVE_ID and
says nothing at all under a uuid strategy.

Each branch now selects a literal naming itself, which is what makes this the
cheapest thing in the library rather than another filter. A constant in the
target list lets Postgres prove the other branch cannot satisfy
`origin = '...'` and drop it — Append node, anti-join and all. `base_only()` is
a plain scan of the base table, with no appendrel left to mis-estimate and
therefore nothing for the nested-loop ban, the m2m fence or the traversal
rewrite to be needed for.

Mostly differential and partition-based rather than SQL-pinning: the populations
have to *partition* the view, because a filter that quietly loses rows or
returns them twice would still look plausible in a table of timings.
"""

import pytest
from django.db import connection, models

from django_overlay import sql as overlay_sql
from tests.testapp.models import Person, PersonUuid4
from tests.testapp_shared.models import PersonSource, PersonSourceUuid4


pytestmark = pytest.mark.django_db


@pytest.fixture
def mixed():
    """All three populations at once, which is what makes the partition tests
    mean anything: a vendor row nobody touched, a vendor row edited here, and a
    row that only ever existed here."""
    untouched = PersonSource.objects.create(first_name="untouched", age=40)
    shadowed = PersonSource.objects.create(first_name="vendor original", age=41)
    # Editing a source row through the view copies it into the base table,
    # which is what "overridden" means.
    overridden = Person.objects.get(pk=-shadowed.id)
    overridden.first_name = "edited here"
    overridden.save()
    organic = Person.objects.create(first_name="ours", age=42)
    return {"untouched": untouched, "overridden": overridden, "organic": organic}


def names(queryset):
    return sorted(queryset.values_list("first_name", flat=True))


# ------------------------------------------------------------ the two free ones


def test_base_only_is_what_the_tenant_holds(mixed):
    assert names(Person.objects.base_only()) == ["edited here", "ours"]


def test_source_only_is_what_nobody_has_touched(mixed):
    assert names(Person.objects.source_only()) == ["untouched"]


def test_the_two_partition_the_view(mixed):
    """Not just "both return something" — together they must be the view
    exactly, with nothing lost and nothing counted twice."""
    everything = names(Person.objects.all())
    assert sorted(names(Person.objects.base_only()) + names(Person.objects.source_only())) == everything
    assert Person.objects.base_only().count() + Person.objects.source_only().count() == Person.objects.count()


# --------------------------------------------------- the two that cost a join


def test_overridden_is_vendor_rows_edited_here(mixed):
    assert names(Person.objects.overridden()) == ["edited here"]


def test_organic_is_rows_that_were_never_the_vendors(mixed):
    assert names(Person.objects.organic()) == ["ours"]


def test_those_two_partition_base_only(mixed):
    """`base_only()` is `organic()` plus `overridden()`, which is the claim in
    organic()'s docstring and the reason neither needs its own branch."""
    assert sorted(names(Person.objects.organic()) + names(Person.objects.overridden())) == names(
        Person.objects.base_only()
    )


def test_a_uuid_strategy_needs_no_negation(mixed):
    """The EXISTS correlates on the pk, and only NEGATIVE_ID negates it. Under a
    uuid strategy the source row keeps its own id, so a `-` spliced in here
    would match nothing and `overridden()` would always be empty."""
    source = PersonSourceUuid4.objects.create(first_name="vendor", age=30)
    row = PersonUuid4.objects.get(pk=source.id)
    row.first_name = "edited"
    row.save()
    PersonUuid4.objects.create(first_name="ours", age=31)

    assert names(PersonUuid4.objects.overridden()) == ["edited"]
    assert names(PersonUuid4.objects.organic()) == ["ours"]


# ------------------------------------------------------------------- reading it


def test_with_origin_says_which_branch_each_row_came_from(mixed):
    rows = {p.first_name: p.overlay_origin for p in Person.objects.with_origin()}
    assert rows == {
        "untouched": overlay_sql.ORIGIN_SOURCE,
        "edited here": overlay_sql.ORIGIN_BASE,
        "ours": overlay_sql.ORIGIN_BASE,
    }


def test_the_alias_can_be_renamed(mixed):
    row = Person.objects.with_origin("came_from").first()
    assert row.came_from in {overlay_sql.ORIGIN_BASE, overlay_sql.ORIGIN_SOURCE}


def test_the_annotation_is_a_column_and_not_just_an_output(mixed):
    """The alias is usable as a column, not only readable off a row.

    A RawSQL annotation is the shape most likely to break here: Django has to
    put the raw fragment itself into the WHERE clause and the GROUP BY, having
    no field to name instead. "How many of these are ours?" is the first
    question anyone asks of an origin column, so it should be one query.
    """
    filtered = Person.objects.with_origin().filter(overlay_origin=overlay_sql.ORIGIN_BASE)
    assert names(filtered) == ["edited here", "ours"]

    counts = dict(
        Person.objects.with_origin()
        .values_list("overlay_origin")
        .order_by("overlay_origin")
        .annotate(n=models.Count("*"))
        .values_list("overlay_origin", "n")
    )
    assert counts == {overlay_sql.ORIGIN_BASE: 2, overlay_sql.ORIGIN_SOURCE: 1}


# ------------------------------------------------- it composes both directions


def test_it_works_from_the_manager_and_mid_chain(mixed):
    """from_queryset puts these on the manager, so both spellings exist and
    both have to mean the same thing."""
    assert names(Person.objects.base_only().filter(age__gte=42)) == ["ours"]
    assert names(Person.objects.filter(age__gte=42).base_only()) == ["ours"]


def test_it_survives_ordering_and_slicing(mixed):
    page = list(Person.objects.base_only().order_by("first_name")[:1])
    assert [p.first_name for p in page] == ["edited here"]


# --------------------------------------------- and the point of the whole thing


def plan_for(queryset):
    sql, params = queryset.query.sql_with_params()
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN " + sql, params)
        return "\n".join(row[0] for row in cursor.fetchall())


def test_base_only_drops_the_other_branch_entirely(mixed):
    """The reason this is worth having, asserted rather than described.

    An unfiltered query over the view is an Append of both branches with the
    anti-join above the source one. Filtering on a per-branch constant lets
    Postgres discard the branch that cannot match, so the Append disappears and
    the anti-join with it -- which is the dominant cost of the whole view.
    """
    assert "Append" in plan_for(Person.objects.all())

    pruned = plan_for(Person.objects.base_only())
    assert "Append" not in pruned, f"the source branch was not pruned:\n{pruned}"
    assert "Anti Join" not in pruned, f"the anti-join survived:\n{pruned}"


def test_source_only_drops_the_base_branch(mixed):
    pruned = plan_for(Person.objects.source_only())
    assert "Append" not in pruned, f"the base branch was not pruned:\n{pruned}"
    # The anti-join belongs to the source branch, so it must still be there --
    # it is what hides rows the base table has taken over.
    assert "Anti Join" in pruned or "NOT EXISTS" in pruned or "SubPlan" in pruned
