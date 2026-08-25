"""Can both branches supply ordered output, and does that give O(limit) paging?

`probe_ordered_paths.py` found the ordered path half-working. With the anti-join
removed (`OverlayMeta.overridable = False`), the planner does choose a
`Merge Append`, and the *source* branch feeds it from an ordered index scan
reading four buffers. The *base* branch does not: it sequentially scans 600,000
rows and top-N sorts them, and because `Merge Append` cannot emit its first row
until both inputs are ordered, that sort is the entire cost.

    ->  Merge Append
          ->  Sort (top-N heapsort)  <- base, 600,000 rows scanned
          ->  Index Scan Backward using src_idx_..._score  <- source, 4 buffers

The base branch carries `WHERE NOT _overlay_deleted`, so a plain btree on
`score` cannot serve it as a pure ordered scan. A **partial** index matching
that predicate can.

If that closes it, unscoped ordered pagination through an overlay view becomes
O(limit) rather than O(rows) -- which is the difference between ~40 seconds and
sub-millisecond at USA scale, and it governs every list screen, not just search.

    OVERLAY_WIDE_SCALE=0.3 OVERLAY_INDEX_SOURCES=1 POSTGRES_USER=postgres \\
        uv run pytest tests/probe_merge_append.py -s -q -o addopts="" --no-cov
"""

import time

import pytest

from tests.probe_ordered_paths import SHAPES, rows_read, shape_of
from tests.probe_search_scaling import best_of, plan, rows, scalar, sql
from tests.probe_uuid7_scale import SCALE, load


pytestmark = pytest.mark.django_db(transaction=True)

BASE = "widecustomer_u7"
SOURCE = "testapp_shared_widecustomeru7source"

QUERIES = {
    "ORDER BY score DESC LIMIT 20": "SELECT id, score FROM shape_{} ORDER BY score DESC LIMIT 20",
    "ORDER BY score DESC LIMIT 20 OFFSET 5000": (
        "SELECT id, score FROM shape_{} ORDER BY score DESC LIMIT 20 OFFSET 5000"
    ),
    "WHERE city=.. ORDER BY score DESC LIMIT 20": (
        "SELECT id, score FROM shape_{} WHERE city = 'city42' ORDER BY score DESC LIMIT 20"
    ),
}


def measure(title):
    print(f"\n  {title}")
    for label, template in QUERIES.items():
        print(f"\n    {label}")
        print(f"      {'shape':>12} {'time':>11} {'plan':>18} {'rows touched':>14}")
        print("      " + "-" * 60)
        for name in SHAPES:
            statement = template.format(name)
            lines = plan(statement)
            print(f"      {name:>12} {best_of(statement):>9.1f}ms {shape_of(lines):>18} {rows_read(lines):>14,}")


def test_merge_append():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s   (scale {SCALE})")

    for name, definition in SHAPES.items():
        sql(f"CREATE VIEW shape_{name} AS {definition}")
    print(f"shape_full: {scalar('SELECT count(*) FROM shape_full'):,} rows")

    print("\n" + "=" * 104)
    print("existing indexes on the base table")
    print("=" * 104)
    for name, definition in rows(
        f"SELECT indexname, indexdef FROM pg_indexes WHERE tablename = '{BASE}' ORDER BY indexname"
    ):
        print(f"  {name:<48} {definition.split(' USING ')[-1][:80]}")

    print("\n" + "=" * 104)
    print("BEFORE: no ordered path on the base branch")
    print("=" * 104)
    measure("baseline")

    print("\n" + "=" * 104)
    print("AFTER: partial index matching the base branch's predicate")
    print("=" * 104)
    print(f"  CREATE INDEX ... ON {BASE} (score DESC) WHERE NOT _overlay_deleted")
    sql(f"CREATE INDEX base_score_live ON {BASE} (score DESC) WHERE NOT _overlay_deleted")
    sql(f"CREATE INDEX base_city_score_live ON {BASE} (city, score DESC) WHERE NOT _overlay_deleted")
    sql(f"CREATE INDEX IF NOT EXISTS src_city_score ON {SOURCE} (city, score DESC)")
    sql(f"ANALYZE {BASE}")
    sql(f"ANALYZE {SOURCE}")
    measure("with partial ordered indexes on both branches")

    print("\n" + "=" * 104)
    print("DOES IT SCALE WITH THE LIMIT, OR WITH THE TABLE? (shape 'none')")
    print("=" * 104)
    print("  O(limit) means these are flat. O(rows) means they track the offset.")
    print(f"\n    {'query':<44} {'time':>11}")
    print("    " + "-" * 58)
    for label, statement in (
        ("LIMIT 20", "SELECT id, score FROM shape_none ORDER BY score DESC LIMIT 20"),
        ("LIMIT 200", "SELECT id, score FROM shape_none ORDER BY score DESC LIMIT 200"),
        ("LIMIT 2000", "SELECT id, score FROM shape_none ORDER BY score DESC LIMIT 2000"),
        ("LIMIT 20 OFFSET 100", "SELECT id, score FROM shape_none ORDER BY score DESC LIMIT 20 OFFSET 100"),
        ("LIMIT 20 OFFSET 100000", "SELECT id, score FROM shape_none ORDER BY score DESC LIMIT 20 OFFSET 100000"),
    ):
        print(f"    {label:<44} {best_of(statement):>9.1f}ms")

    print("\n" + "=" * 104)
    print("full plan: shape 'none', ORDER BY score DESC LIMIT 20, after the index")
    print("=" * 104)
    for line in plan("SELECT id, score FROM shape_none ORDER BY score DESC LIMIT 20")[:18]:
        print("   ", line[:150])

    print("\n" + "=" * 104)
    print("full plan: shape 'full' (anti-join present), same query")
    print("=" * 104)
    for line in plan("SELECT id, score FROM shape_full ORDER BY score DESC LIMIT 20")[:18]:
        print("   ", line[:150])

    for index in ("base_score_live", "base_city_score_live", "src_city_score"):
        sql(f"DROP INDEX IF EXISTS {index}")
    for name in SHAPES:
        sql(f"DROP VIEW IF EXISTS shape_{name}")
