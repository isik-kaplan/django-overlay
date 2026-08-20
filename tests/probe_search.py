"""Full-text and fuzzy search through an overlay view.

Untested territory until now, and the questions are specific:

  1. Does a GIN index get used on **both** branches, or does one side fall
     back to a sequential scan?
  2. What does ranked search cost against a plain table holding identical rows?
  3. Is `ts_rank` really comparable across the two branches? It should be —
     it scores from term frequency and position *within the document*, with no
     corpus statistics — but "should be" is what got us twice today.
  4. Does an override correctly shadow its source row in search results?
  5. What does it cost to index only one branch, which is the easy mistake?

Not run by CI or the default suite:

    OVERLAY_WIDE_SCALE=0.3 POSTGRES_USER=postgres uv run pytest \\
        tests/probe_search.py -s -q -o addopts="" --no-cov
"""

import time

import pytest
from django.db import connection

from tests.probe_uuid7_scale import load


pytestmark = pytest.mark.django_db(transaction=True)

VIEW = "widecustomer_u7_view"
BASE = "widecustomer_u7"
SOURCE = "testapp_shared_widecustomeru7source"
PLAIN = "bu7_customer"

# 'simple' rather than 'english': these are names, and stemming "Downing" to
# "down" is not what anyone wants from a person search.
TSV = "to_tsvector('simple', first_name || ' ' || last_name || ' ' || city || ' ' || status)"


def sql(statement):
    with connection.cursor() as cursor:
        cursor.execute(statement)


def rows(statement):
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return cursor.fetchall()


def plan(statement):
    return [row[0] for row in rows("EXPLAIN (ANALYZE, BUFFERS) " + statement)]


def scans(lines):
    """(gin index scans, sequential scans) in the plan."""
    gin = sum(1 for line in lines if "Bitmap Index Scan" in line or "Index Scan" in line)
    seq = sum(1 for line in lines if "Seq Scan" in line)
    return gin, seq


def exec_ms(lines):
    for line in lines:
        if "Execution Time" in line:
            return float(line.split(":")[1].strip().split()[0])
    return float("nan")


def best_of(statement, rounds=3):
    best = None
    for _ in range(rounds):
        started = time.perf_counter()
        rows(statement)
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return best


def report(label, statement):
    lines = plan(statement)
    gin, seq = scans(lines)
    print(f"  {label:<46} {best_of(statement):>9.1f}ms   idx={gin} seq={seq}   {exec_ms(lines):>8.1f}ms in-plan")
    return lines


def ranked(relation, query, limit=50):
    return (
        f"SELECT id, ts_rank({TSV}, to_tsquery('simple', '{query}')) AS r "  # noqa: S608 - fixed identifiers
        f"FROM {relation} WHERE {TSV} @@ to_tsquery('simple', '{query}') "
        f"ORDER BY r DESC LIMIT {limit}"
    )


def test_search():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s")
    total = rows(f"SELECT count(*) FROM {VIEW}")[0][0]
    print(f"{VIEW}: {total:,} rows\n")

    sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    print("=" * 100)
    print("1. only the BASE branch indexed — the easy mistake")
    print("=" * 100)
    sql(f"CREATE INDEX srch_base ON {BASE} USING GIN ({TSV})")
    sql(f"ANALYZE {BASE}")
    report("narrow FTS, one branch indexed", ranked(VIEW, "last42"))

    print("\n" + "=" * 100)
    print("2. BOTH branches indexed — does each side use its own index?")
    print("=" * 100)
    sql(f"CREATE INDEX srch_src ON {SOURCE} USING GIN ({TSV})")
    sql(f"CREATE INDEX srch_plain ON {PLAIN} USING GIN ({TSV})")
    for table in (SOURCE, PLAIN):
        sql(f"ANALYZE {table}")

    for label, query in (
        ("narrow  (last42)", "last42"),
        ("mid     (city42)", "city42"),
        ("broad   (active)", "active"),
    ):
        print(f"\n  --- {label}")
        report("overlay view", ranked(VIEW, query))
        report("plain table", ranked(PLAIN, query))

    print("\n" + "=" * 100)
    print("3. trigram / fuzzy name search")
    print("=" * 100)
    # Distinctive rows, inserted before the trigram section so fuzzy search has
    # something realistic to match. Every generated last_name shares the prefix
    # "last", which makes trigram similarity high across the whole table and
    # tells you nothing about whether the index works.
    sql(
        f"INSERT INTO {SOURCE} (id, first_name, last_name, email, age, city, postcode, status, "
        f"score, registered_on, notes) VALUES "
        f"('00000000-0000-7000-8000-00000000aaaa', 'Zebediah', 'Quixotic', 'z@x', 1, 'nowhere', "
        f"'zz', 'active', 1, DATE '2020-01-01', '')"
    )
    sql(
        f"INSERT INTO {BASE} (id, first_name, last_name, email, age, city, postcode, status, "
        f"score, registered_on, notes, _overlay_deleted) VALUES "
        f"('00000000-0000-7000-8000-00000000bbbb', 'Zebediah', 'Quixotic', 'z@x', 1, 'nowhere', "
        f"'zz', 'active', 1, DATE '2020-01-01', '', FALSE)"
    )

    sql(f"CREATE INDEX trgm_base ON {BASE} USING GIN (last_name gin_trgm_ops)")
    sql(f"CREATE INDEX trgm_src ON {SOURCE} USING GIN (last_name gin_trgm_ops)")
    sql(f"CREATE INDEX trgm_plain ON {PLAIN} USING GIN (last_name gin_trgm_ops)")
    for table in (BASE, SOURCE, PLAIN):
        sql(f"ANALYZE {table}")

    def fuzzy(relation, term):
        return (
            f"SELECT id, similarity(last_name, '{term}') AS s FROM {relation} "  # noqa: S608 - fixed identifiers
            # A single %, not %%: nothing is passed as a parameter here, so
            # psycopg does no interpolation and %% would reach Postgres literally.
            f"WHERE last_name % '{term}' ORDER BY s DESC LIMIT 50"
        )

    print("  --- 'last42': CONFOUNDED, every generated last_name starts with 'last'")
    report("overlay view", fuzzy(VIEW, "last42"))
    report("plain table", fuzzy(PLAIN, "last42"))
    print("  --- 'Quixotc': a realistic typo against a distinctive name")
    report("overlay view", fuzzy(VIEW, "Quixotc"))
    report("plain table", fuzzy(PLAIN, "Quixotc"))

    print("\n" + "=" * 100)
    print("4. is ts_rank comparable across the two branches?")
    print("=" * 100)
    # The two Quixotic rows inserted above carry identical text on both sides.
    # If ts_rank used corpus statistics they would score differently, since the
    # branches are wildly different sizes.
    scored = rows(
        f"SELECT id, ts_rank({TSV}, to_tsquery('simple', 'Quixotic')) AS r "  # noqa: S608 - fixed identifiers
        f"FROM {VIEW} WHERE {TSV} @@ to_tsquery('simple', 'Quixotic') ORDER BY id"
    )
    print(f"  rows found: {len(scored)}")
    for row_id, rank in scored:
        branch = "base  " if str(row_id).endswith("bbbb") else "source"
        print(f"    {branch}  {row_id}  ts_rank={rank}")
    distinct_scores = {float(r) for _, r in scored}
    print(f"  distinct scores: {distinct_scores}  ->  {'COMPARABLE' if len(distinct_scores) == 1 else 'DIFFERENT'}")

    print("\n" + "=" * 100)
    print("5. does an override shadow its source row in search results?")
    print("=" * 100)
    src_id = rows(
        f"SELECT id FROM {SOURCE} s WHERE last_name = 'last42' "  # noqa: S608 - fixed identifiers
        f"AND NOT EXISTS (SELECT 1 FROM {BASE} b WHERE b.id = s.id) LIMIT 1"
    )[0][0]
    before = rows(f"SELECT count(*) FROM {VIEW} WHERE {TSV} @@ to_tsquery('simple', 'last42')")[0][0]  # noqa: S608
    sql(
        f"INSERT INTO {BASE} (id, first_name, last_name, email, age, city, postcode, status, score, "
        f"registered_on, notes, _overlay_deleted) "
        f"SELECT id, first_name, 'CorrectedName', email, age, city, postcode, status, score, "
        f"registered_on, notes, FALSE FROM {SOURCE} WHERE id = '{src_id}'"
    )
    after = rows(f"SELECT count(*) FROM {VIEW} WHERE {TSV} @@ to_tsquery('simple', 'last42')")[0][0]  # noqa: S608
    corrected = rows(
        f"SELECT count(*) FROM {VIEW} WHERE {TSV} @@ to_tsquery('simple', 'CorrectedName')"  # noqa: S608
    )[0][0]
    print(f"  matches for 'last42' before override : {before}")
    print(f"  matches for 'last42' after override  : {after}   (expected {before - 1})")
    print(f"  matches for 'CorrectedName'          : {corrected}   (expected 1)")
    print(f"  -> {'CORRECT' if after == before - 1 and corrected == 1 else 'WRONG'}")

    print("\n" + "=" * 100)
    print("full plan: ranked FTS through the view")
    print("=" * 100)
    for line in plan(ranked(VIEW, "last42"))[:22]:
        print("   ", line[:150])

    for index in ("srch_base", "srch_src", "srch_plain", "trgm_base", "trgm_src", "trgm_plain"):
        sql(f"DROP INDEX IF EXISTS {index}")
