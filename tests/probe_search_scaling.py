"""Where does ranked search stop being instant?

`probe_search.py` established that search through the view works: both branches
use their own GIN index, `ts_rank` is comparable across them, and overrides
shadow correctly. What it did not establish is the shape of the cost curve, and
that is the number the OpenSearch decision turns on.

The structural claim being tested: Postgres full-text search has **no top-k
early termination**. A GIN index finds every matching row, the executor scores
all of them, then sorts and takes the limit. Cost should therefore scale with
the number of *matches* and be almost independent of the LIMIT. Lucene, which
is what OpenSearch runs, does block-max WAND -- it skips documents that
provably cannot reach the top k -- so its cost scales with the limit instead.

If that claim holds, the decision boundary is a match count, not a row count.
A distinctive name stays instant at any table size; a common one does not.

Method: write a frequency ladder into `notes` on both branches and on the plain
mirror. Token `mD` is present when a hash of the row's id is divisible by D, so
`m1` matches everything, `m2` half, `m4` a quarter, down to a handful of rows.
Hashing the id rather than the load counter keeps the frequencies independent
between the two branches and uniform across them.

Then measure, for every rung:

  1. ranked top-20 through the view, and against the plain mirror
  2. the same query unranked, to separate finding matches from scoring them
  3. the same query at LIMIT 20 / 200 / 2000, to test limit-independence

Run at two scales; the curve should move with matches and not with table size.

    OVERLAY_WIDE_SCALE=0.3 POSTGRES_USER=postgres uv run pytest \\
        tests/probe_search_scaling.py -s -q -o addopts="" --no-cov
"""

import time

import pytest
from django.db import connection

from tests.probe_uuid7_scale import SCALE, load


pytestmark = pytest.mark.django_db(transaction=True)

VIEW = "widecustomer_u7_view"
BASE = "widecustomer_u7"
SOURCE = "testapp_shared_widecustomeru7source"
PLAIN = "bu7_customer"

TSV = "to_tsvector('simple', notes)"

# 28 bits off md5, so the value is always positive and uniform. Deriving the
# frequency from the id rather than the load counter means the two branches get
# independent draws, which is what a real corpus looks like.
HASH = "(('x0' || substr(md5(id::text), 1, 7))::bit(32)::int)"

DIVISORS = (1, 2, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144)


def notes_expression() -> str:
    # A single %, not %%. Django only runs %-interpolation when `params` is not
    # None, and nothing here passes parameters, so %% would reach Postgres
    # literally and fail with "operator does not exist: integer %% integer".
    arms = " ".join(f"CASE WHEN {HASH} % {d} = 0 THEN 'm{d}' END," for d in DIVISORS)
    return f"concat_ws(' ', {arms.rstrip(',')})"


def sql(statement):
    with connection.cursor() as cursor:
        cursor.execute(statement)


def rows(statement):
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return cursor.fetchall()


def scalar(statement):
    return rows(statement)[0][0]


def plan(statement):
    return [row[0] for row in rows("EXPLAIN (ANALYZE, BUFFERS) " + statement)]


def scans(lines):
    idx = sum(1 for line in lines if "Bitmap Index Scan" in line or "Index Scan" in line)
    seq = sum(1 for line in lines if "Seq Scan" in line)
    return idx, seq


def best_of(statement, rounds=3, give_up_after_ms=4000):
    best = None
    for _ in range(rounds):
        started = time.perf_counter()
        rows(statement)
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
        if best > give_up_after_ms:
            break
    return best


def ranked(relation, token, limit=20):
    return (
        f"SELECT id, ts_rank({TSV}, to_tsquery('simple', '{token}')) AS r "  # noqa: S608 - fixed identifiers
        f"FROM {relation} WHERE {TSV} @@ to_tsquery('simple', '{token}') "
        f"ORDER BY r DESC LIMIT {limit}"
    )


def unranked(relation, token, limit=20):
    return (
        f"SELECT id FROM {relation} "  # noqa: S608 - fixed identifiers
        f"WHERE {TSV} @@ to_tsquery('simple', '{token}') LIMIT {limit}"
    )


def paint_notes():
    """The frequency ladder, written identically into all three relations."""
    expression = notes_expression()
    for table in (SOURCE, BASE, PLAIN):
        sql(f"UPDATE {table} SET notes = {expression}")  # noqa: S608 - fixed identifiers
    for table in (SOURCE, BASE, PLAIN):
        sql(f"CREATE INDEX srchs_{table[-14:]} ON {table} USING GIN ({TSV})")
        sql(f"ANALYZE {table}")


TSV_VIEW = "srch_tsv_view"


def stored_tsvector_comparison():
    """Same rows, same query, tsvector stored instead of recomputed.

    The view is hand-rolled rather than reusing the library's, because the real
    view's select list is fixed by the model's columns and would not expose a
    generated column. Its shape is copied from view.sql.j2 under a uuid
    strategy: base rows minus tombstones, UNION ALL, source rows anti-joined on
    a plain id equality.
    """
    for table in (SOURCE, BASE, PLAIN):
        sql(
            f"ALTER TABLE {table} ADD COLUMN notes_tsv tsvector "
            f"GENERATED ALWAYS AS (to_tsvector('simple', notes)) STORED"
        )
        sql(f"CREATE INDEX srchv_{table[-14:]} ON {table} USING GIN (notes_tsv)")
        sql(f"ANALYZE {table}")

    sql(
        f"CREATE VIEW {TSV_VIEW} AS "  # noqa: S608 - fixed identifiers
        f"SELECT id, notes, notes_tsv FROM {BASE} WHERE NOT _overlay_deleted "
        f"UNION ALL "
        f"SELECT id, notes, notes_tsv FROM {SOURCE} s "
        f"WHERE NOT EXISTS (SELECT 1 FROM {BASE} b WHERE b.id = s.id)"
    )

    def stored(relation, token, limit=20):
        return (
            f"SELECT id, ts_rank(notes_tsv, to_tsquery('simple', '{token}')) AS r "  # noqa: S608
            f"FROM {relation} WHERE notes_tsv @@ to_tsquery('simple', '{token}') "
            f"ORDER BY r DESC LIMIT {limit}"
        )

    print(
        f"\n  {'token':>8} {'matches':>10} | {'view expr':>11} {'view stored':>12} {'gain':>7}"
        f" | {'plain expr':>11} {'plain stored':>13} {'gain':>7}"
    )
    print("  " + "-" * 96)
    for divisor in DIVISORS:
        token = f"m{divisor}"
        matches = scalar(
            f"SELECT count(*) FROM {TSV_VIEW} WHERE notes_tsv @@ to_tsquery('simple', '{token}')"  # noqa: S608
        )
        if matches == 0:
            continue
        view_expr = best_of(ranked(VIEW, token))
        view_stored = best_of(stored(TSV_VIEW, token))
        plain_expr = best_of(ranked(PLAIN, token))
        plain_stored = best_of(stored(PLAIN, token))
        print(
            f"  {token:>8} {matches:>10,} | {view_expr:>9.1f}ms {view_stored:>10.1f}ms "
            f"{view_expr / view_stored:>6.1f}x | {plain_expr:>9.1f}ms {plain_stored:>11.1f}ms "
            f"{plain_expr / plain_stored:>6.1f}x"
        )

    sql(f"DROP VIEW IF EXISTS {TSV_VIEW}")
    for table in (SOURCE, BASE, PLAIN):
        sql(f"DROP INDEX IF EXISTS srchv_{table[-14:]}")
        sql(f"ALTER TABLE {table} DROP COLUMN notes_tsv")


def test_search_scaling():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s   (scale {SCALE})")

    total = scalar(f"SELECT count(*) FROM {VIEW}")
    print(f"{VIEW}: {total:,} rows")

    started = time.perf_counter()
    paint_notes()
    print(f"ladder painted + indexed in {time.perf_counter() - started:.0f}s\n")

    print("=" * 104)
    print("RANKED TOP-20: cost against match count")
    print("=" * 104)
    print(f"  {'token':>8} {'matches':>10} {'% of view':>10} {'overlay':>11} {'plain':>11} {'ratio':>8}   {'plan':>12}")
    print("  " + "-" * 100)

    curve = []
    for divisor in DIVISORS:
        token = f"m{divisor}"
        matches = scalar(
            f"SELECT count(*) FROM {VIEW} WHERE {TSV} @@ to_tsquery('simple', '{token}')"  # noqa: S608
        )
        if matches == 0:
            continue
        overlay_ms = best_of(ranked(VIEW, token))
        plain_ms = best_of(ranked(PLAIN, token))
        idx, seq = scans(plan(ranked(VIEW, token)))
        ratio = overlay_ms / plain_ms if plain_ms else float("nan")
        share = 100.0 * matches / total
        print(
            f"  {token:>8} {matches:>10,} {share:>9.2f}% {overlay_ms:>9.1f}ms "
            f"{plain_ms:>9.1f}ms {ratio:>7.2f}x   idx={idx} seq={seq}"
        )
        curve.append((token, matches, overlay_ms, plain_ms))

    print("\n" + "=" * 104)
    print("RANKED vs UNRANKED: how much of the cost is scoring and sorting?")
    print("=" * 104)
    print(f"  {'token':>8} {'matches':>10} {'ranked':>11} {'unranked':>11} {'sort cost':>12}")
    print("  " + "-" * 60)
    for token, matches, overlay_ms, _ in curve:
        bare_ms = best_of(unranked(VIEW, token))
        print(
            f"  {token:>8} {matches:>10,} {overlay_ms:>9.1f}ms {bare_ms:>9.1f}ms "
            f"{overlay_ms / bare_ms if bare_ms else float('nan'):>11.1f}x"
        )

    print("\n" + "=" * 104)
    print("LIMIT-INDEPENDENCE: does asking for fewer rows cost less?")
    print("=" * 104)
    print("  (Lucene's WAND makes this scale with the limit. Postgres should be flat.)")
    print(f"\n  {'token':>8} {'matches':>10} {'LIMIT 20':>11} {'LIMIT 200':>11} {'LIMIT 2000':>11}")
    print("  " + "-" * 60)
    for token, matches, _, _ in curve:
        if matches < 2000:
            continue
        times = [best_of(ranked(VIEW, token, limit)) for limit in (20, 200, 2000)]
        print(f"  {token:>8} {matches:>10,} " + " ".join(f"{t:>9.1f}ms" for t in times))

    print("\n" + "=" * 104)
    print("EXTRAPOLATION: ms per 1,000 matches (ranked top-20, overlay)")
    print("=" * 104)
    for token, matches, overlay_ms, plain_ms in curve:
        if matches < 100:
            continue
        print(
            f"  {token:>8} {matches:>10,} matches   overlay {1000 * overlay_ms / matches:>7.3f} ms/1k"
            f"   plain {1000 * plain_ms / matches:>7.3f} ms/1k"
        )

    print("\n" + "=" * 104)
    print("SCOPED SEARCH: a broad term plus a selective filter")
    print("=" * 104)
    print("  Real search screens are scoped -- by city, status, region. If the filter")
    print("  collapses the match count, a broad term stops being the worst case.")
    print(f"\n  {'token':>8} {'unscoped':>11} {'+city':>11} {'+city+status':>14}")
    print("  " + "-" * 50)
    for token, matches, overlay_ms, _ in curve:
        if matches < 10_000:
            continue
        scoped = (
            f"SELECT id, ts_rank({TSV}, to_tsquery('simple', '{token}')) AS r "  # noqa: S608
            f"FROM {VIEW} WHERE {TSV} @@ to_tsquery('simple', '{token}') "
            f"AND city = 'city42' ORDER BY r DESC LIMIT 20"
        )
        tighter = scoped.replace("ORDER BY", "AND status = 'active' ORDER BY")
        print(f"  {token:>8} {overlay_ms:>9.1f}ms {best_of(scoped):>9.1f}ms {best_of(tighter):>12.1f}ms")

    print("\n" + "=" * 104)
    print("STORED TSVECTOR: is ranking paying to recompute to_tsvector per row?")
    print("=" * 104)
    print("  An expression index makes the *lookup* fast but stores nothing in the heap,")
    print("  so ts_rank has to rebuild the tsvector from `notes` for every matching row.")
    print("  A STORED generated column pays that once, at write time.")
    stored_tsvector_comparison()

    print("\n" + "=" * 104)
    print("full plan: ranked top-20, mid-selectivity token")
    print("=" * 104)
    mid = next((t for t, m, _, _ in curve if m > 3000), "m256")
    for line in plan(ranked(VIEW, mid))[:24]:
        print("   ", line[:150])

    for table in (SOURCE, BASE, PLAIN):
        sql(f"DROP INDEX IF EXISTS srchs_{table[-14:]}")
