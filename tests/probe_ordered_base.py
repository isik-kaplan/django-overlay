"""Why did the base branch refuse the ordered index, when the source branch took it?

`probe_merge_append.py` showed the mechanism works: with `(city, score DESC)` on
both branches and no anti-join, a scoped ordered query went 62.8ms -> 0.3ms via
`Merge Append` with real early termination.

The *unscoped* `ORDER BY score DESC LIMIT 20` did not. A partial index
`(score DESC) WHERE NOT _overlay_deleted` was created and ignored, and the base
branch kept sequentially scanning 600,000 rows to feed a top-N heapsort, while
the source branch happily used `Index Scan Backward` on its plain score index.

Two candidate explanations, and they have different consequences:

  A. The index is unusable on its own terms -- the branch query alone will not
     use it either. Then it is an indexing problem and fixable.
  B. The branch query alone *does* use it, and only the appendrel context
     suppresses it. Then it is the same planner-estimation hole that produced
     `rows=1` on the anti-join, and indexing cannot fix it.

Isolates the base branch, the source branch, and the union, at each step.

    OVERLAY_WIDE_SCALE=0.3 OVERLAY_INDEX_SOURCES=1 POSTGRES_USER=postgres \\
        uv run pytest tests/probe_ordered_base.py -s -q -o addopts="" --no-cov
"""

import time

import pytest

from tests.probe_search_scaling import best_of, plan, rows, sql
from tests.probe_uuid7_scale import SCALE, load


pytestmark = pytest.mark.django_db(transaction=True)

BASE = "widecustomer_u7"
SOURCE = "testapp_shared_widecustomeru7source"


def access(lines):
    """The access method the plan actually used, in one word."""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("->") or stripped.startswith("Limit") is False:
            if "Index Scan Backward" in line:
                return "IndexScanBackward"
            if "Index Only Scan" in line:
                return "IndexOnlyScan"
            if "Index Scan" in line:
                return "IndexScan"
            if "Seq Scan" in line:
                return "SeqScan"
    return "?"


def sorted_(lines):
    return "yes" if any("Sort Method" in line for line in lines) else "no"


def show(label, statement):
    lines = plan(statement)
    print(f"  {label:<52} {best_of(statement):>8.1f}ms   {access(lines):>18}   sort={sorted_(lines)}")
    return lines


def test_ordered_base():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s   (scale {SCALE})\n")

    base_branch = (
        f"SELECT id, score FROM {BASE} WHERE NOT _overlay_deleted ORDER BY score DESC LIMIT 20"
    )
    source_branch = f"SELECT id, score FROM {SOURCE} ORDER BY score DESC LIMIT 20"
    union = (
        f"SELECT id, score FROM ("
        f"SELECT id, score FROM {BASE} WHERE NOT _overlay_deleted "
        f"UNION ALL SELECT id, score FROM {SOURCE}) u ORDER BY score DESC LIMIT 20"
    )

    print("=" * 100)
    print("STEP 1: no ordered index on the base table")
    print("=" * 100)
    show("base branch alone", base_branch)
    show("source branch alone", source_branch)
    show("union of both", union)

    print("\n" + "=" * 100)
    print("STEP 2: partial index (score DESC) WHERE NOT _overlay_deleted")
    print("=" * 100)
    sql(f"CREATE INDEX base_score_partial ON {BASE} (score DESC) WHERE NOT _overlay_deleted")
    sql(f"ANALYZE {BASE}")
    show("base branch alone", base_branch)
    show("union of both", union)

    print("\n" + "=" * 100)
    print("STEP 3: plain index (score DESC), no predicate")
    print("=" * 100)
    sql(f"CREATE INDEX base_score_plain ON {BASE} (score DESC)")
    sql(f"ANALYZE {BASE}")
    show("base branch alone", base_branch)
    show("union of both", union)

    print("\n" + "=" * 100)
    print("STEP 4: covering index -- (score DESC) INCLUDE (id), no heap fetch")
    print("=" * 100)
    sql(f"CREATE INDEX base_score_cover ON {BASE} (score DESC) INCLUDE (id) "
        f"WHERE NOT _overlay_deleted")
    sql(f"CREATE INDEX src_score_cover ON {SOURCE} (score DESC) INCLUDE (id)")
    sql(f"ANALYZE {BASE}")
    sql(f"ANALYZE {SOURCE}")
    show("base branch alone", base_branch)
    show("source branch alone", source_branch)
    show("union of both", union)

    print("\n" + "=" * 100)
    print("indexes now on the base table")
    print("=" * 100)
    for (name, definition) in rows(
        f"SELECT indexname, indexdef FROM pg_indexes WHERE tablename = '{BASE}' "
        f"AND indexname LIKE 'base_score%' ORDER BY indexname"
    ):
        print(f"  {name:<28} {definition.split(' USING ')[-1][:90]}")

    print("\n" + "=" * 100)
    print("full plan: union, final state")
    print("=" * 100)
    for line in plan(union)[:20]:
        print("   ", line[:150])

    for index in ("base_score_partial", "base_score_plain", "base_score_cover", "src_score_cover"):
        sql(f"DROP INDEX IF EXISTS {index}")
