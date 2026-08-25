"""Does the anti-join block index-ordered early termination?

`probe_search_ordering.py` tested the hypothesis that ordering by an indexed
column gives Postgres genuine top-k early termination through the overlay. It
did not: every plan was a top-N heapsort over a `Parallel Append`, and even
`ORDER BY score DESC LIMIT 20` with no filter took 123ms and scanned both
branches end to end.

The plan says why:

    Parallel Hash Anti Join  (cost=... rows=1 ...) (actual ... rows=100000 loops=3)

A hash anti-join produces unordered output, so the source branch cannot feed a
`Merge Append`. And the estimate is wrong by five orders of magnitude, so the
planner has no reason to cost an ordered alternative in the first place.

That is testable. `OverlayMeta.overridable = False` removes the anti-join
entirely, and soft-delete narrows it to tombstones. If the anti-join is what
blocks the ordered path, the three view shapes should behave completely
differently on the same data and the same query.

Shapes, matching sql.anti_join_kind():

    full        base UNION ALL source WHERE NOT EXISTS (any base row)
    tombstones  base UNION ALL source WHERE NOT EXISTS (a *deleted* base row)
    none        base UNION ALL source

Also runs a diagnostic with `enable_sort = off`, purely to ask whether an
ordered plan *exists* and what it would cost. That is a measurement device, not
a proposed setting.

    OVERLAY_WIDE_SCALE=0.3 OVERLAY_INDEX_SOURCES=1 POSTGRES_USER=postgres \\
        uv run pytest tests/probe_ordered_paths.py -s -q -o addopts="" --no-cov
"""

import time

import pytest

from tests.probe_search_scaling import best_of, plan, scalar, sql
from tests.probe_uuid7_scale import SCALE, load


pytestmark = pytest.mark.django_db(transaction=True)

BASE = "widecustomer_u7"
SOURCE = "testapp_shared_widecustomeru7source"

COLUMNS = "id, score, city, last_name"

SHAPES = {
    "full": (
        f"SELECT {COLUMNS} FROM {BASE} WHERE NOT _overlay_deleted "
        f"UNION ALL "
        f"SELECT {COLUMNS} FROM {SOURCE} s "
        f"WHERE NOT EXISTS (SELECT 1 FROM {BASE} b WHERE b.id = s.id)"
    ),
    "tombstones": (
        f"SELECT {COLUMNS} FROM {BASE} WHERE NOT _overlay_deleted "
        f"UNION ALL "
        f"SELECT {COLUMNS} FROM {SOURCE} s "
        f"WHERE NOT EXISTS (SELECT 1 FROM {BASE} b WHERE b.id = s.id AND b._overlay_deleted)"
    ),
    "none": (f"SELECT {COLUMNS} FROM {BASE} WHERE NOT _overlay_deleted UNION ALL SELECT {COLUMNS} FROM {SOURCE} s"),
}


def shape_of(lines):
    joined = "\n".join(lines)
    if "Merge Append" in joined:
        return "MergeAppend" if "Sort Method" not in joined else "MergeAppend+sort"
    if "Sort Method" in joined:
        return "sort"
    return "append"


def rows_read(lines):
    """Peak `rows=` seen on any scan node -- how much the plan actually touched."""
    peak = 0
    for line in lines:
        if "actual" in line and "rows=" in line.split("actual")[1]:
            tail = line.split("actual")[1]
            for chunk in tail.split("rows=")[1:]:
                digits = ""
                for character in chunk:
                    if character.isdigit():
                        digits += character
                    else:
                        break
                if digits:
                    peak = max(peak, int(digits))
    return peak


def test_ordered_paths():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s   (scale {SCALE})")

    for name, definition in SHAPES.items():
        sql(f"CREATE VIEW shape_{name} AS {definition}")
    sql(f"ANALYZE {BASE}")
    sql(f"ANALYZE {SOURCE}")
    total = scalar("SELECT count(*) FROM shape_full")
    print(f"shape_full: {total:,} rows\n")

    queries = {
        "ORDER BY score DESC LIMIT 20": "SELECT id, score FROM shape_{} ORDER BY score DESC LIMIT 20",
        "ORDER BY score DESC LIMIT 20 OFFSET 5000": (
            "SELECT id, score FROM shape_{} ORDER BY score DESC LIMIT 20 OFFSET 5000"
        ),
        "WHERE city=.. ORDER BY score DESC LIMIT 20": (
            "SELECT id, score FROM shape_{} WHERE city = 'city42' ORDER BY score DESC LIMIT 20"
        ),
    }

    print("=" * 104)
    print("THREE VIEW SHAPES, SAME DATA, SAME QUERY")
    print("=" * 104)
    for label, template in queries.items():
        print(f"\n  {label}")
        print(f"    {'shape':>12} {'time':>11} {'plan':>18} {'rows touched':>14}")
        print("    " + "-" * 60)
        for name in SHAPES:
            statement = template.format(name)
            lines = plan(statement)
            print(f"    {name:>12} {best_of(statement):>9.1f}ms {shape_of(lines):>18} {rows_read(lines):>14,}")

    print("\n" + "=" * 104)
    print("DIAGNOSTIC: does an ordered plan even exist? (enable_sort = off)")
    print("=" * 104)
    print("  Asking the planner what it was refusing to consider. Not a proposed setting.")
    print(f"\n    {'shape':>12} {'default':>11} {'no-sort':>11} {'no-sort plan':>18} {'rows touched':>14}")
    print("    " + "-" * 72)
    statement_template = "SELECT id, score FROM shape_{} ORDER BY score DESC LIMIT 20"
    for name in SHAPES:
        statement = statement_template.format(name)
        default_ms = best_of(statement)
        sql("SET enable_sort = off")
        try:
            lines = plan(statement)
            forced_ms = best_of(statement)
            print(
                f"    {name:>12} {default_ms:>9.1f}ms {forced_ms:>9.1f}ms {shape_of(lines):>18} {rows_read(lines):>14,}"
            )
        finally:
            sql("SET enable_sort = on")

    print("\n" + "=" * 104)
    print("full plan: shape 'none', ORDER BY score DESC LIMIT 20")
    print("=" * 104)
    for line in plan("SELECT id, score FROM shape_none ORDER BY score DESC LIMIT 20")[:18]:
        print("   ", line[:150])

    print("\n" + "=" * 104)
    print("full plan: shape 'none' with enable_sort = off")
    print("=" * 104)
    sql("SET enable_sort = off")
    try:
        for line in plan("SELECT id, score FROM shape_none ORDER BY score DESC LIMIT 20")[:18]:
            print("   ", line[:150])
    finally:
        sql("SET enable_sort = on")

    for name in SHAPES:
        sql(f"DROP VIEW IF EXISTS shape_{name}")
