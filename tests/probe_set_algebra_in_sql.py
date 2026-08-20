"""Can the leaf-by-leaf strategy stay in the database?

probe_staged_resolution found that resolving each m2m leaf separately and
intersecting the id sets in Python is the only strategy that survives three
leaves -- 2,213ms where everything else runs past 20s. The cost is client
memory: ~19MB per broad leaf, ~57MB for three.

The obvious reading of that result was "Django had to pull the ids into
Python". That reading is wrong. `filter(pk__in=<queryset>)` compiles to a SQL
subquery and moves nothing, and probe_staged_resolution's `subquery-first`
variant did exactly that -- and still ran past the cap. What the Python round
trip actually bought was not data movement but *planner information*: a
literal list of 200 ids has an exact, visible cardinality, and a subquery over
a UNION ALL view has none.

So the question is whether anything gives the planner that same visible
cardinality without leaving the database. Two candidates:

  fenced-array   `= ANY (ARRAY(subquery))` is an InitPlan -- evaluated once,
                 before the outer plan is costed, so the outer query sees a
                 constant array rather than a relation it cannot size. This is
                 the m2m fence's own mechanism, applied to whole leaves.
  INTERSECT      set operations plan each branch separately, which is the very
                 property that makes leaf-by-leaf work in Python. If that
                 survives being expressed as SQL, the memory ceiling is gone
                 and the result stays a lazy queryset.

Both are measured against the Python intersection that already works.

    OVERLAY_BENCH_SCALE=1.0 POSTGRES_USER=postgres uv run pytest \\
        --reuse-db tests/probe_set_algebra_in_sql.py -s -q -o addopts="" --no-cov
"""

import time

import pytest
from django.db import OperationalError, connection
from django.db.models import Q

from tests.probe_bench_graph import load
from tests.testapp.models import BenchPerson, PlainPerson


pytestmark = pytest.mark.django_db(transaction=True)

CAP_MS = 20_000

LEAVES = (
    {"addresses__city": "city0"},
    {"phones__kind": "mobile"},
    {"emails__domain": "example.com"},
)


def cap(milliseconds):
    with connection.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = {milliseconds}")
        cursor.execute("SET lock_timeout = 5000")


def python_intersection(model, leaves):
    """The known-good baseline: every leaf resolved, combined as Python sets."""
    resolved = None
    for leaf in leaves:
        found = set(model.objects.filter(**leaf).values_list("pk", flat=True))
        resolved = found if resolved is None else (resolved & found)
    return len(resolved)


def fenced_arrays(model, leaves):
    """One `= ANY (ARRAY(subquery))` per leaf, all in one statement.

    Each InitPlan is evaluated once and collapses to a constant array, so the
    outer query gets a countable predicate per leaf instead of a join it cannot
    estimate -- the same trick the m2m fence plays, applied a level up.
    """
    queryset = model.objects.all()
    for leaf in leaves:
        inner = model.objects.filter(**leaf).values("pk")
        queryset = queryset.filter(Q(pk__overlay_fenced_in=inner))
    return queryset.values("pk").distinct().count()


def plain_subqueries(model, leaves):
    """The same shape with ordinary `pk__in`, as the control."""
    queryset = model.objects.all()
    for leaf in leaves:
        queryset = queryset.filter(Q(pk__in=model.objects.filter(**leaf).values("pk")))
    return queryset.values("pk").distinct().count()


def sql_intersect(model, leaves):
    """`INTERSECT` between the leaves, planned branch by branch."""
    branches = [model.objects.filter(**leaf).values("pk").distinct() for leaf in leaves]
    combined = branches[0].intersection(*branches[1:])
    return len(list(combined))


STRATEGIES = (
    ("python intersection", python_intersection, "both"),
    ("fenced arrays (SQL)", fenced_arrays, "overlay"),
    ("plain pk__in (SQL)", plain_subqueries, "both"),
    ("INTERSECT (SQL)", sql_intersect, "both"),
)

CASES = (("2 leaves", LEAVES[:2]), ("3 leaves", LEAVES))


def timed(build):
    started = time.perf_counter()
    try:
        value = build()
    except OperationalError:
        return float(CAP_MS), None
    return (time.perf_counter() - started) * 1000, value


def test_set_algebra_in_sql():
    load()
    cap(CAP_MS)

    for case_label, leaves in CASES:
        print("\n\n" + "=" * 104)
        print(f"{case_label}: " + " AND ".join(next(iter(leaf)) for leaf in leaves))
        print("=" * 104)
        print(f"  {'strategy':<26} {'overlay':>11} {'plain':>10} {'ratio':>9}   people")
        print("  " + "-" * 90)

        truth = None
        for label, strategy, sides in STRATEGIES:
            overlay_ms, overlay_people = timed(lambda s=strategy, ls=leaves: s(BenchPerson, ls))
            if sides == "both":
                plain_ms, plain_people = timed(lambda s=strategy, ls=leaves: s(PlainPerson, ls))
            else:
                # `overlay_fenced_in` resolves only through OverlayQuery, by
                # design -- a plain model has no way to express this row.
                plain_ms, plain_people = float("nan"), None
            if truth is None and plain_people is not None:
                truth = plain_people

            if overlay_people is None:
                cells = f"{'>' + str(CAP_MS // 1000) + 's':>11} "
            else:
                cells = f"{overlay_ms:>9.0f}ms "
            cells += "         -" if sides != "both" else f"{plain_ms:>8.0f}ms"
            if sides == "both" and overlay_people is not None and plain_ms:
                cells += f" x{overlay_ms / plain_ms:>8.1f}"
            else:
                cells += f" {'-':>9}"

            note = "did not finish" if overlay_people is None else f"{overlay_people:,}"
            if overlay_people is not None and truth is not None and overlay_people != truth:
                note += f"   WRONG (expected {truth:,})"
            print(f"  {label:<26} {cells}   {note}")
