"""Why does adding a *more* selective condition make the query 17x slower?

`probe_multi_m2m` measures, on the same data:

    two broad m2m                      1,095ms
    two broad m2m + a 2.5%% scope      18,336ms      <- more selective, 17x slower
    two broad m2m + a 0.01%% scope        353ms
    two broad + 2.5%% scope, .distinct()   486ms      <- 38x faster than without

What the plans show, which is not what I first guessed. The trigger is the
LIMIT alone, and the ORDER BY is innocent:

    ORDER BY id LIMIT 200      timeout at 30s
    ORDER BY id, no LIMIT           2,440ms   <- same work, finishes fine
    LIMIT 200, no ORDER BY     timeout at 30s
    .distinct(), LIMIT 200            545ms

The mechanism is the join estimate. The top Merge Join estimates **542 billion**
rows and produces 1,488 — off by 364,000,000x. A LIMIT makes the planner scale
every plan's cost by `limit / estimated_rows`, so at 200/542e9 it concludes that
*any* plan will stop after a rounding error's worth of work, and it picks one
built entirely of nested loops with no hash anywhere. There are only 1,488
matching rows in total, so the early exit it was counting on never arrives and
the nested loops run to exhaustion. Remove the LIMIT and the planner has to
cost the whole result honestly, so it builds the hash and finishes in 2.4s.

`.distinct()` rescues it for the same reason: dedup cannot be done lazily, so
the trick is unavailable and the honest plan wins.

The estimate is bad for a reason this project has already met: the views are
`UNION ALL` appendrels, `examine_simple_variable()` has no arm for a subquery
RTE with `inh` set, so every column falls back to DEFAULT_NUM_DISTINCT. One
join survives that; two compound it into the 542-billion-row fantasy.

    OVERLAY_BENCH_SCALE=0.3 POSTGRES_USER=postgres uv run pytest \\
        tests/probe_limit_trap.py -s -q -o addopts="" --no-cov
"""

import pytest
from django.db import connection

from tests.probe_bench_graph import load
from tests.testapp.models import BenchPerson


pytestmark = pytest.mark.django_db(transaction=True)

BROAD_A = {"addresses__country": "US"}
BROAD_B = {"phones__kind": "mobile"}
SELECTIVE = {"city__in": [f"city{n}" for n in range(25)]}


def explain(queryset):
    """(top node, estimated rows, actual rows, milliseconds) for a queryset."""
    sql, params = queryset.query.sql_with_params()
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = 30000")
        cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", params)
        plan = cursor.fetchone()[0][0]
        cursor.execute("SET statement_timeout = 0")
    root = plan["Plan"]
    return plan, root


def walk(node, depth=0, out=None):
    out = [] if out is None else out
    estimated, actual = node.get("Plan Rows", 0), node.get("Actual Rows", 0)
    ratio = estimated / actual if actual else float("inf")
    out.append(
        f"    {'  ' * depth}{node['Node Type']:<28} "
        f"est {estimated:>9,}  actual {actual:>9,}  off by {ratio:>8.1f}x"
    )
    for child in node.get("Plans", [])[:3]:
        walk(child, depth + 1, out)
    return out


def test_limit_trap():
    load()
    base = BROAD_A | BROAD_B | SELECTIVE

    shapes = (
        ("two broad only, ORDER BY id LIMIT 200",
         BenchPerson.objects.filter(**(BROAD_A | BROAD_B)).order_by("id")[:200]),
        ("+ 2.5% scope, ORDER BY id LIMIT 200   <- the slow one",
         BenchPerson.objects.filter(**base).order_by("id")[:200]),
        ("+ 2.5% scope, .distinct()",
         BenchPerson.objects.filter(**base).distinct().order_by("id")[:200]),
        ("+ 2.5% scope, no LIMIT",
         BenchPerson.objects.filter(**base).order_by("id")),
        ("+ 2.5% scope, no ORDER BY",
         BenchPerson.objects.filter(**base)[:200]),
    )

    print("\n\n" + "=" * 100)
    print("IS IT THE LIMIT, THE ORDER BY, OR BOTH?")
    print("=" * 100)
    for label, queryset in shapes:
        try:
            plan, root = explain(queryset)
        except Exception as error:  # noqa: BLE001 - a timeout here is a result
            print(f"\n  {label}\n    {type(error).__name__}: {str(error).strip()[:70]}")
            continue
        print(f"\n  {label}")
        print(f"    {'total':<28} {plan['Execution Time']:>9.1f}ms")
        for line in walk(root):
            print(line)
