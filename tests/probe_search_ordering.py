"""Postgres *does* have top-k early termination -- just not for `ts_rank`.

`probe_search_scaling.py` showed that ordering by a computed relevance score
costs the same whether you ask for 20 rows or 2000, because every match must be
scored before anything can be sorted. That is what makes an unscoped search over
a common term expensive, and it is the whole case for Lucene.

But an `ORDER BY` that an **index can supply** is a different plan entirely: the
executor walks the index in order, applies the filter as it goes, and stops as
soon as it has the limit. No full scan, no sort. And it should compose with the
overlay, because a `UNION ALL` of two index-ordered branches is a `Merge Append`,
which preserves order and stops early too.

If that holds, the two strategies are complementary in exactly the right way:

  - a **rare** term is cheap under `ts_rank` (few matches to score) and expensive
    under index ordering (you walk a long way to find 20 hits)
  - a **common** term is expensive under `ts_rank` (millions of matches) and
    cheap under index ordering (the first 20 rows you touch already match)

The expensive case for one is the cheap case for the other. This measures both
across the full selectivity ladder to see whether the crossover is real.

Needs source-side indexes, or the source branch cannot supply ordered output:

    OVERLAY_WIDE_SCALE=0.3 OVERLAY_INDEX_SOURCES=1 POSTGRES_USER=postgres \\
        uv run pytest tests/probe_search_ordering.py -s -q -o addopts="" --no-cov
"""

import time

import pytest

from tests.probe_search_scaling import (
    DIVISORS,
    PLAIN,
    TSV,
    VIEW,
    best_of,
    notes_expression,
    plan,
    scalar,
    scans,
    sql,
)
from tests.probe_uuid7_scale import SCALE, load


pytestmark = pytest.mark.django_db(transaction=True)

BASE = "widecustomer_u7"
SOURCE = "testapp_shared_widecustomeru7source"


def by_rank(relation, token, limit=20):
    return (
        f"SELECT id, ts_rank({TSV}, to_tsquery('simple', '{token}')) AS r "  # noqa: S608
        f"FROM {relation} WHERE {TSV} @@ to_tsquery('simple', '{token}') "
        f"ORDER BY r DESC LIMIT {limit}"
    )


def by_index(relation, token, limit=20):
    """Same filter, but ordered by something a btree can hand over in order."""
    return (
        f"SELECT id, score FROM {relation} "  # noqa: S608
        f"WHERE {TSV} @@ to_tsquery('simple', '{token}') "
        f"ORDER BY score DESC LIMIT {limit}"
    )


def ordering_shape(lines):
    merge = any("Merge Append" in line for line in lines)
    sort = any(line.strip().startswith("->  Sort") or "Sort Method" in line for line in lines)
    if merge and not sort:
        return "MergeAppend"
    if merge:
        return "MergeAppend+sort"
    return "sort" if sort else "append"


def test_ordering_strategies():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s   (scale {SCALE})")
    total = scalar(f"SELECT count(*) FROM {VIEW}")
    print(f"{VIEW}: {total:,} rows")

    expression = notes_expression()
    for table in (SOURCE, BASE, PLAIN):
        sql(f"UPDATE {table} SET notes = {expression}")  # noqa: S608
        sql(f"CREATE INDEX srcho_{table[-14:]} ON {table} USING GIN ({TSV})")
        sql(f"ANALYZE {table}")
    print("ladder painted + indexed\n")

    print("=" * 108)
    print("RELEVANCE ORDER vs INDEX ORDER, same filter, same LIMIT 20")
    print("=" * 108)
    print(f"  {'token':>9} {'matches':>10} {'% view':>8} | {'ORDER BY ts_rank':>18} "
          f"| {'ORDER BY score':>16} {'plan':>16} | {'speedup':>9}")
    print("  " + "-" * 104)

    for divisor in DIVISORS:
        token = f"m{divisor}"
        matches = scalar(
            f"SELECT count(*) FROM {VIEW} WHERE {TSV} @@ to_tsquery('simple', '{token}')"  # noqa: S608
        )
        if matches == 0:
            continue
        rank_ms = best_of(by_rank(VIEW, token))
        index_ms = best_of(by_index(VIEW, token))
        shape = ordering_shape(plan(by_index(VIEW, token)))
        share = 100.0 * matches / total
        print(f"  {token:>9} {matches:>10,} {share:>7.2f}% | {rank_ms:>16.1f}ms "
              f"| {index_ms:>14.1f}ms {shape:>16} | {rank_ms / index_ms:>8.1f}x")

    print("\n" + "=" * 108)
    print("PURE INDEX ORDER: no text filter at all, just ordered pagination")
    print("=" * 108)
    print("  The ceiling for the index-ordered strategy, and the shape a")
    print("  'browse by rank' screen would use.")
    for label, statement in (
        ("ORDER BY score DESC LIMIT 20", f"SELECT id, score FROM {VIEW} ORDER BY score DESC LIMIT 20"),
        ("ORDER BY score DESC LIMIT 20 OFFSET 10000",
         f"SELECT id, score FROM {VIEW} ORDER BY score DESC LIMIT 20 OFFSET 10000"),
        ("equality + ordered", f"SELECT id, score FROM {VIEW} WHERE city = 'city42' "
                               f"ORDER BY score DESC LIMIT 20"),
    ):
        lines = plan(statement)
        idx, seq = scans(lines)
        print(f"  {label:<44} {best_of(statement):>9.1f}ms   {ordering_shape(lines):>16}   "
              f"idx={idx} seq={seq}")

    print("\n" + "=" * 108)
    print("full plan: broad term, index-ordered")
    print("=" * 108)
    for line in plan(by_index(VIEW, "m1"))[:20]:
        print("   ", line[:150])

    for table in (SOURCE, BASE, PLAIN):
        sql(f"DROP INDEX IF EXISTS srcho_{table[-14:]}")
