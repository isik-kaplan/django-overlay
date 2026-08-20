"""Where is the cliff? Two m2m conditions, swept from narrow to broad.

Every earlier measurement of a two-m2m filter used `addresses__country`, which
has four distinct values -- so it matched a fifth of everyone and was barely a
filter at all. A real geographic condition is a *county*, and there are
thousands of those. The whole "two m2m conditions do not finish at 1,000,000
people" result may be an artifact of picking the broadest possible column.

The bench address table has no county, but its `city` carries 1,000 distinct
values, which is the right order for one. So sweep it: one city, then five,
then twenty-five, then a hundred, then the old four-valued country for
reference. Each is ANDed with `phones__kind='mobile'`, which is the second
condition from the real query.

Two things are measured per point, because a target list is used both ways:

  resolve   the distinct person ids the search matches -- what building or
            counting a saved list does, and what everything downstream
            consumes.
  summarise the twenty-aggregate panel over that scope, written the way
            probe_aggregation found to be best (buckets join-free, one query
            per relation, scope attached as a subquery).

The timeout is deliberately set to four minutes rather than a probe-sized few
seconds: the system this is standing in for takes ten to fifteen minutes today,
so "slower than four minutes" is the answer, not a measurement failure. A cell
that caps out is a cell that missed the bar.

    OVERLAY_BENCH_SCALE=1.0 POSTGRES_USER=postgres uv run pytest \\
        --reuse-db tests/probe_selectivity_sweep.py -s -q -o addopts="" --no-cov
"""

import os
import time

import pytest
from django.db import OperationalError, connection
from django.db.models import Count

from tests.probe_aggregation import RELATIONS, buckets_for
from tests.probe_bench_graph import load
from tests.testapp.models import BenchPerson, PlainPerson


pytestmark = pytest.mark.django_db(transaction=True)

# Four minutes. Not a probe timeout -- a pass/fail line. See the module docstring.
TIMEOUT_MS = int(os.environ.get("OVERLAY_SWEEP_TIMEOUT_MS", 240_000))

PHONES = {"phones__kind": "mobile"}

# Narrow to broad. `city` stands in for a county: 1,000 distinct values against
# a real county count in the low thousands.
#
# The one-city point is the least representative of the five. The generator
# assigns cities on `g %% 1000` and picks each link's source-vs-organic side on
# `g %% 2`, and 1,000 is even -- so every address in a single city shares a
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


def resolve(model, scope):
    """The distinct person ids the search matches."""
    return len(set(model.objects.filter(**scope).values_list("pk", flat=True)))


def summarise(model, scope):
    """The twenty-aggregate panel, in the shape probe_aggregation found best."""
    scoped = {"pk__in": model.objects.filter(**scope).values("pk")}
    summary = model.objects.filter(**scoped).aggregate(**buckets_for(distinct=False))
    for alias, path in RELATIONS.items():
        summary |= model.objects.filter(**scoped).aggregate(**{alias: Count(path, distinct=True)})
    return summary["total"]


def timed(build):
    """(milliseconds, value). One round: at four minutes a best-of-two costs
    more wall clock than the extra precision is worth."""
    started = time.perf_counter()
    try:
        value = build()
    except OperationalError:
        return float(TIMEOUT_MS), None
    return (time.perf_counter() - started) * 1000, value


def test_selectivity_sweep():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s")
    cap_the_session_at(TIMEOUT_MS)
    print(f"  {BenchPerson.objects.count():,} people in the view")
    print(f"  cap is {TIMEOUT_MS / 1000:.0f}s -- a capped cell missed the bar, "
          f"it is not a broken measurement")

    for operation_label, operation in (("RESOLVE the matching ids", resolve),
                                       ("SUMMARISE over that scope", summarise)):
        print("\n" + "=" * 104)
        print(f"{operation_label}   (each ANDed with phones__kind='mobile')")
        print("=" * 104)
        print(f"  {'condition':<30} {'overlay':>12} {'plain':>11} {'ratio':>9}   people")
        print("  " + "-" * 92)

        # Narrow to broad, and stop escalating the overlay once it caps: every
        # broader point is strictly more work, so the cliff is already located
        # and the remaining cells would only burn four minutes each to say so.
        overlay_capped = False
        for label, condition in SELECTIVITIES:
            scope = condition | PHONES
            if overlay_capped:
                overlay_ms, people = float("nan"), None
                skipped = True
            else:
                overlay_ms, people = timed(lambda s=scope, op=operation: op(BenchPerson, s))
                skipped = False
                overlay_capped = people is None
            plain_ms, plain_people = timed(lambda s=scope, op=operation: op(PlainPerson, s))

            if skipped:
                cells = f"{'not run':>12} {plain_ms:>9.0f}ms {'':>9}"
                note = "skipped: broader than the first cap"
            elif people is None:
                cells = f"{'>' + str(TIMEOUT_MS // 1000) + 's':>12} {plain_ms:>9.0f}ms {'':>9}"
                note = f"MISSED THE BAR   ({plain_people:,} people)"
            else:
                ratio = overlay_ms / plain_ms if plain_ms else float("nan")
                cells = f"{overlay_ms:>10.0f}ms {plain_ms:>9.0f}ms x{ratio:>8.1f}"
                note = f"{people:,}"
                if plain_people is not None and people != plain_people:
                    note += f"   ROWS DIFFER (plain {plain_people:,})"
            print(f"  {label:<30} {cells}   {note}")


def cap_the_session_at(timeout_ms):
    """Bound every statement, not just the measured ones.

    probe_aggregation has the same guard with its own, shorter cap; the lesson
    behind both is written up there. The short version: an uncapped unmeasured
    query once held ACCESS SHARE on these views for over an hour and every
    later run's TRUNCATE queued behind it, because killing pytest does not
    cancel a running backend.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = {timeout_ms}")
        cursor.execute("SET lock_timeout = 5000")
