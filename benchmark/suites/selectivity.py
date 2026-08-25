"""Where is the cliff? Two m2m conditions, swept from narrow to broad.

Every early measurement of a two-m2m filter used `addresses__country`, which
has four distinct values -- so it matched a fifth of everyone and was barely a
filter at all. A real geographic condition is a *county*, and there are
thousands of those. The whole "two m2m conditions do not finish at 1,000,000
people" result could have been an artifact of picking the broadest possible
column, and this is what rules that out.

The bench address table has no county, but its `city` carries 1,000 distinct
values, which is the right order for one. So sweep it: one city, then five,
then twenty-five, then a hundred, then the old four-valued country for
reference. Each is ANDed with `phones__kind='mobile'`, the second condition
from the query this stands in for.

Two things are measured per point, because a target list is used both ways:

  resolve   the distinct person ids the search matches -- what building or
            counting a saved list does, and what everything downstream
            consumes.
  summarise the twenty-aggregate panel over that scope, written the way the
            aggregation suite found to be best.
"""

from django.db.models import Count

from benchmark import harness
from benchmark.suites.aggregation import RELATIONS, buckets_for


NAME = "selectivity"
TITLE = "Where the cliff is: two m2m conditions from narrow to broad"

PHONES = {"phones__kind": "mobile"}

# Narrow to broad. `city` stands in for a county: 1,000 distinct values against
# a real county count in the low thousands.
#
# The one-city point is the least representative of the five. The generator
# assigns cities on `g % 1000` and picks each link's source-vs-organic side on
# `g % 2`, and 1,000 is even -- so every address in a single city shares a
# parity and therefore a side. Taking a *range* of cities spans both. The
# matched-people column is printed for every row so any such skew is visible
# rather than inferred.
SELECTIVITIES = (
    ("1 city", {"addresses__city__in": [f"city{n}" for n in range(1)]}),
    ("5 cities", {"addresses__city__in": [f"city{n}" for n in range(5)]}),
    ("25 cities", {"addresses__city__in": [f"city{n}" for n in range(25)]}),
    ("100 cities", {"addresses__city__in": [f"city{n}" for n in range(100)]}),
    ("country='US' (the old one)", {"addresses__country": "US"}),
)

COLUMNS = ("overlay", "plain", "ratio", "people")


def resolve(model, scope):
    """The distinct person ids the search matches."""
    return len(set(model.objects.filter(**scope).values_list("pk", flat=True)))


def summarise(model, scope):
    """The twenty-aggregate panel, in the shape the aggregation suite found best."""
    scoped = {"pk__in": model.objects.filter(**scope).values("pk")}
    summary = model.objects.filter(**scoped).aggregate(**buckets_for(distinct=False))
    for alias, path in RELATIONS.items():
        summary |= model.objects.filter(**scoped).aggregate(**{alias: Count(path, distinct=True)})
    return summary["total"]


def run(ctx):
    from tests.testapp.models import BenchPerson, PlainPerson

    for operation_label, operation in (("Resolve the matching ids", resolve), ("Summarise over that scope", summarise)):
        section = harness.Section(
            f"{operation_label} (each ANDed with phones__kind='mobile')",
            COLUMNS,
            note="a capped cell missed the bar; it is not a broken measurement",
        )

        # Narrow to broad, and stop escalating the overlay once it caps: every
        # broader point is strictly more work, so the cliff is already located
        # and the remaining cells would only burn the full cap each to say so.
        overlay_capped = False
        for label, condition in SELECTIVITIES:
            scope = condition | PHONES
            if overlay_capped:
                plain, plain_people = ctx.measure(lambda s=scope, op=operation: op(PlainPerson, s), rounds=2)
                section.add(
                    label,
                    {"plain": plain},
                    overlay="not run",
                    ratio="",
                    people=f"skipped: broader than the first cap ({plain_people:,} plain)"
                    if plain_people
                    else "skipped",
                )
                continue

            overlay, people = ctx.measure(lambda s=scope, op=operation: op(BenchPerson, s), rounds=2)
            overlay_capped = people is None
            plain, plain_people = ctx.measure(lambda s=scope, op=operation: op(PlainPerson, s), rounds=2)

            ctx.compare(f"{operation_label} / {label}", people, plain_people)

            ratio = ""
            if not overlay.capped and not plain.capped and plain.ms:
                ratio = f"x{overlay.ms / plain.ms:.1f}"
            section.add(
                label,
                {"overlay": overlay, "plain": plain},
                ratio=ratio,
                people="MISSED THE BAR" if people is None else f"{people:,}",
            )
        yield section
