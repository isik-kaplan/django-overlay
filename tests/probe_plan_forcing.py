"""Keep the single model: fix the plan instead of the estimate.

Two overlay views joined together do not finish at 1,000,000 people --
probe_narrow_m2m_stall measured the planner estimating 267,425,037,000 rows
for a 132-row answer. The estimate cannot be fixed: an appendrel parent has no
statistics, `examine_simple_variable()` has no arm for one, and nothing in
Postgres lets you supply them.

But every failure has the same *plan* signature, not just the same estimate: a
huge estimate makes the cheapest-startup path a nested loop, `add_path()`
prefers it, and it then runs to exhaustion because the early exit it was
costed on never arrives. A wrong estimate is only fatal when it selects an
unbounded plan.

So try forbidding the plan rather than correcting the estimate. Everything here
is plan-level and returns identical rows, which matters: it is the same bar the
library already applies to decide what it may do automatically.

  enable_nestloop=off    the direct one. Hash and merge joins are O(n); a
                         nested loop over a mis-estimated relation is not.
  + enable_memoize=off   memoize sits on top of a nested loop and can make the
                         planner like it even more, so try removing both.
  join_collapse_limit=1  plan the joins in written order instead of searching,
                         so one bad estimate cannot reorder the whole query.
  OFFSET 0 in the view   an optimisation fence that blocks subquery pull-up. If
                         the view is never flattened into an appendrel, its
                         branches are planned separately and the parent may get
                         a real row count. Cheapest possible fix if it works --
                         one line in the view template.

    OVERLAY_BENCH_SCALE=1.0 POSTGRES_USER=postgres uv run pytest \\
        --reuse-db tests/probe_plan_forcing.py -s -q -o addopts="" --no-cov
"""

import time

import pytest
from django.db import OperationalError, connection

from tests.probe_bench_graph import load
from tests.testapp.models import BenchPerson, PlainPerson


pytestmark = pytest.mark.django_db(transaction=True)

CAP_MS = 20_000

# The known-bad shape: two m2m conditions, both targeting overlay views.
SCOPE = {"addresses__city": "city0", "phones__kind": "mobile"}

SETTINGS = (
    ("baseline (nothing forced)", []),
    ("enable_nestloop = off", ["SET enable_nestloop = off"]),
    ("nestloop + memoize off", ["SET enable_nestloop = off", "SET enable_memoize = off"]),
    ("join_collapse_limit = 1", ["SET join_collapse_limit = 1", "SET from_collapse_limit = 1"]),
    ("nestloop off + collapse 1",
     ["SET enable_nestloop = off", "SET join_collapse_limit = 1", "SET from_collapse_limit = 1"]),
)

RESET = (
    "SET enable_nestloop = on",
    "SET enable_memoize = on",
    "SET join_collapse_limit = 8",
    "SET from_collapse_limit = 8",
)


def run(statements):
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


def cap(milliseconds):
    run([f"SET statement_timeout = {milliseconds}", "SET lock_timeout = 5000"])


def resolve(model):
    return model.objects.filter(**SCOPE).values("pk").distinct().count()


def estimate(model):
    statement, params = model.objects.filter(**SCOPE).values("pk").query.sql_with_params()
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN (FORMAT JSON) {statement}", params)
        root = cursor.fetchone()[0][0]["Plan"]
    return root["Node Type"], root["Plan Rows"]


def timed(build):
    started = time.perf_counter()
    try:
        value = build()
    except OperationalError:
        return float(CAP_MS), None
    return (time.perf_counter() - started) * 1000, value


def fence_the_view():
    """Rebuild bench_person_view with an OFFSET 0 pull-up fence.

    Hand-rolled rather than routed through the library: the point is to find
    out whether the fence is worth adding to the view template at all, and
    rebuilding one view here answers that without touching sql_templates.

    Only the person view is fenced. If the idea works at all it should show up
    with one of the three views fenced, and rebuilding all of them risks
    confusing "the fence helped" with "something else changed".
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_viewdef('bench_person_view', true)")
        definition = cursor.fetchone()[0].rstrip().rstrip(";")
        cursor.execute(
            f"CREATE OR REPLACE VIEW bench_person_view AS "
            f"SELECT * FROM ({definition}) AS fenced OFFSET 0"
        )


def test_plan_forcing():
    load()
    cap(CAP_MS)

    truth = PlainPerson.objects.filter(**SCOPE).values("pk").distinct().count()
    print(f"\n\n  the answer is {truth:,} people "
          f"(plain tables, {timed(lambda: resolve(PlainPerson))[0]:.0f}ms)")

    print("\n" + "=" * 100)
    print("FORCING THE PLAN, WITHOUT TOUCHING THE ESTIMATE")
    print("=" * 100)
    print(f"  {'setting':<32} {'top node':<20} {'est rows':>18} {'overlay':>10}   people")
    print("  " + "-" * 92)

    for label, statements in SETTINGS:
        run(RESET)
        run(statements)
        cap(CAP_MS)
        node, rows = estimate(BenchPerson)
        elapsed, people = timed(lambda: resolve(BenchPerson))
        cell = f"{'>' + str(CAP_MS // 1000) + 's':>10}" if people is None else f"{elapsed:>8.0f}ms"
        verdict = "capped" if people is None else f"{people:,}"
        if people is not None and people != truth:
            verdict += f"   WRONG (expected {truth:,})"
        print(f"  {label:<32} {node:<20} {rows:>18,} {cell}   {verdict}")

    run(RESET)
    cap(CAP_MS)
    print("\n" + "=" * 100)
    print("OFFSET 0 IN THE VIEW: DOES BLOCKING PULL-UP GIVE THE PARENT A ROW COUNT?")
    print("=" * 100)
    fence_the_view()
    node, rows = estimate(BenchPerson)
    elapsed, people = timed(lambda: resolve(BenchPerson))
    cell = f"{'>' + str(CAP_MS // 1000) + 's':>10}" if people is None else f"{elapsed:>8.0f}ms"
    verdict = "capped" if people is None else f"{people:,}"
    if people is not None and people != truth:
        verdict += f"   WRONG (expected {truth:,})"
    print(f"  {'person view fenced':<32} {node:<20} {rows:>18,} {cell}   {verdict}")
    print("\n  (the view is rebuilt in-place; the next load() restores it)")
