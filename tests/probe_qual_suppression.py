"""Is a restriction qual on one branch what suppresses the ordered path?

`probe_ordered_base.py` isolated it. Both branches produce ordered output in
0.2ms standalone -- index scan, no sort -- with any index shape. Inside a
`UNION ALL` the source branch keeps its `Index Only Scan` while the base branch
is forced through a `Seq Scan` + top-N sort of all 600,000 rows, and no index
(partial, plain, or covering) changes that.

The only structural difference between the two branches in that test is that the
base side carries `WHERE NOT _overlay_deleted` and the source side carries
nothing.

That maps onto a real configuration rather than a curiosity. `soft_delete` is
what puts the qual there; without it the base branch is an unqualified scan, the
same shape as the source branch. So:

    filtered    base WHERE NOT _overlay_deleted  UNION ALL  source
    unfiltered  base                             UNION ALL  source

If `unfiltered` gets `Merge Append` over two ordered scans and `filtered` does
not, the qual is the cause, `soft_delete = False` is the workaround, and a
composite index leading with the qual column is the candidate real fix -- which
is the third case here.

    OVERLAY_WIDE_SCALE=0.3 OVERLAY_INDEX_SOURCES=1 POSTGRES_USER=postgres \\
        uv run pytest tests/probe_qual_suppression.py -s -q -o addopts="" --no-cov
"""

import time

import pytest

from tests.probe_ordered_base import access, sorted_
from tests.probe_search_scaling import best_of, plan, sql
from tests.probe_uuid7_scale import SCALE, load


pytestmark = pytest.mark.django_db(transaction=True)

BASE = "widecustomer_u7"
SOURCE = "testapp_shared_widecustomeru7source"

FILTERED = (
    f"SELECT id, score FROM ("
    f"SELECT id, score FROM {BASE} WHERE NOT _overlay_deleted "
    f"UNION ALL SELECT id, score FROM {SOURCE}) u ORDER BY score DESC LIMIT 20"
)
UNFILTERED = (
    f"SELECT id, score FROM ("
    f"SELECT id, score FROM {BASE} "
    f"UNION ALL SELECT id, score FROM {SOURCE}) u ORDER BY score DESC LIMIT 20"
)


def merge_append(lines):
    return "yes" if any("Merge Append" in line for line in lines) else "no"


def show(label, statement):
    lines = plan(statement)
    print(
        f"  {label:<46} {best_of(statement):>8.1f}ms   {access(lines):>18}   "
        f"sort={sorted_(lines):<4} mergeappend={merge_append(lines)}"
    )
    return lines


def test_qual_suppression():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s   (scale {SCALE})\n")

    sql(f"CREATE INDEX qs_src_score ON {SOURCE} (score DESC) INCLUDE (id)")
    sql(f"CREATE INDEX qs_base_score ON {BASE} (score DESC) INCLUDE (id)")
    sql(f"ANALYZE {BASE}")
    sql(f"ANALYZE {SOURCE}")

    print("=" * 104)
    print("A: plain ordered index on both branches")
    print("=" * 104)
    show("union, base branch UNFILTERED", UNFILTERED)
    show("union, base branch WHERE NOT _overlay_deleted", FILTERED)

    print("\n" + "=" * 104)
    print("B: composite index leading with the qual column")
    print("=" * 104)
    print(f"  CREATE INDEX ... ON {BASE} (_overlay_deleted, score DESC) INCLUDE (id)")
    sql(f"CREATE INDEX qs_base_del_score ON {BASE} (_overlay_deleted, score DESC) INCLUDE (id)")
    sql(f"ANALYZE {BASE}")
    show("union, base branch WHERE NOT _overlay_deleted", FILTERED)

    print("\n" + "=" * 104)
    print("C: the qual rewritten as an equality the index can seek on")
    print("=" * 104)
    equality = FILTERED.replace("WHERE NOT _overlay_deleted", "WHERE _overlay_deleted = FALSE")
    show("union, base branch WHERE _overlay_deleted = FALSE", equality)

    print("\n" + "=" * 104)
    print("D: does it hold up as a paging query?")
    print("=" * 104)
    for label, statement in (
        ("unfiltered, LIMIT 20", UNFILTERED),
        ("unfiltered, LIMIT 20 OFFSET 100000", UNFILTERED.replace("LIMIT 20", "LIMIT 20 OFFSET 100000")),
        ("equality qual, LIMIT 20", equality),
        ("equality qual, LIMIT 20 OFFSET 100000", equality.replace("LIMIT 20", "LIMIT 20 OFFSET 100000")),
    ):
        print(f"  {label:<46} {best_of(statement):>8.1f}ms")

    print("\n" + "=" * 104)
    print("full plan: best case")
    print("=" * 104)
    best = UNFILTERED if best_of(UNFILTERED) <= best_of(equality) else equality
    for line in plan(best)[:18]:
        print("   ", line[:150])

    for index in ("qs_src_score", "qs_base_score", "qs_base_del_score"):
        sql(f"DROP INDEX IF EXISTS {index}")
