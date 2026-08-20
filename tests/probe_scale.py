"""Does the overlay view perform roughly like a single indexed table?

Not run by CI, and not run by the default suite — it loads a few hundred
thousand rows. Run it deliberately:

    POSTGRES_USER=postgres uv run pytest tests/probe_scale.py -s -q \
        -o addopts="" --no-cov

    OVERLAY_BENCH_ROWS=2000000 POSTGRES_USER=postgres uv run pytest ...

It uses the real `Person` overlay model, so the view, the `UNION ALL`, the
anti-join and the INSTEAD OF triggers are the ones the library generates. The
comparison is `bench_plain`, an ordinary table holding the same rows with the
same indexes — what you would have written if you didn't need an overlay.

Each measurement is best-of-7 to take the cache out of it, and every query is
run through the ORM so the SQL is what an application would actually send.
"""

import os
import time

import pytest
from django.db import connection

from tests.testapp.models import Person


pytestmark = pytest.mark.django_db(transaction=True)

ROWS = int(os.environ.get("OVERLAY_BENCH_ROWS", 400_000))
HALF = ROWS // 2


def sql(statement, *params):
    with connection.cursor() as cursor:
        cursor.execute(statement, params or None)


def best_of(fn, rounds=7):
    best = None
    for _ in range(rounds):
        started = time.perf_counter()
        fn()
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return best


def plan(query):
    """The top line of the plan, which is what tells you whether the view is
    being scanned or merged."""
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN {query}")
        rows = [row[0] for row in cursor.fetchall()]
    for row in rows:
        stripped = row.strip()
        if stripped.startswith("->"):
            continue
        return stripped[:76]
    return rows[0].strip()[:76]


def reset():
    """TRUNCATE, not queryset.delete(). Deleting through the view runs the
    INSTEAD OF DELETE trigger once per row — 200k rows takes minutes, which is
    a finding in its own right and is why bulk deletes want another mechanism.
    Deferred FK triggers have to be flushed first or TRUNCATE refuses."""
    sql("SET CONSTRAINTS ALL IMMEDIATE")
    sql("TRUNCATE person, testapp_shared_personsource CASCADE")
    sql("SET CONSTRAINTS ALL DEFERRED")


@pytest.fixture
def loaded(db_cursor):
    """Half the rows materialised in the base table, half showing through from
    the source — the shape that makes the view do the most work."""
    reset()
    sql("DROP TABLE IF EXISTS bench_plain")

    sql(
        "INSERT INTO person (id, first_name, age, _overlay_deleted) "
        "SELECT g, 'name' || g, g %% 90, FALSE FROM generate_series(1, %s) g",
        HALF,
    )
    sql(
        "INSERT INTO testapp_shared_personsource (id, first_name, age) "
        "SELECT g, 'name' || g, g %% 90 FROM generate_series(%s, %s) g",
        HALF + 1,
        ROWS,
    )
    sql("CREATE TABLE bench_plain (id bigint PRIMARY KEY, first_name varchar(100) NOT NULL, age integer)")
    sql(
        "INSERT INTO bench_plain (id, first_name, age) SELECT g, 'name' || g, g %% 90 FROM generate_series(1, %s) g",
        ROWS,
    )

    # The rule from PERFORMANCE-TODO: matching indexes on *both* sides, or the
    # planner cannot merge them and falls back to scanning.
    for table in ("person", "testapp_shared_personsource", "bench_plain"):
        sql(f"CREATE INDEX IF NOT EXISTS bench_{table}_age ON {table} (age)")
        sql(f"CREATE INDEX IF NOT EXISTS bench_{table}_age_id ON {table} (age, id)")
    # The rows above were seeded with explicit ids, so the sequence the insert
    # benchmark draws from is still at 1.
    sql(
        "SELECT setval(seq, %s) FROM (SELECT pg_get_serial_sequence('person', 'id') AS seq) s WHERE seq IS NOT NULL",
        ROWS + 1000,
    )
    sql("ANALYZE person")
    sql("ANALYZE testapp_shared_personsource")
    sql("ANALYZE bench_plain")

    yield

    sql("DROP TABLE IF EXISTS bench_plain")
    reset()


def measure(results, key):
    """Run every comparison and record it under `key`."""

    def compare(label, overlay, plain, note=""):
        # A query that matches nothing is fast and means nothing, and the
        # negated id space makes that an easy mistake to make. Check first.
        for side, fn in (("overlay", overlay), ("plain", plain)):
            outcome = fn()
            if isinstance(outcome, list):
                assert outcome, f"{label} ({side}) returned no rows -- it would time an empty query"

        results.setdefault(label, {"note": note})[key] = best_of(overlay)
        results[label].setdefault("plain", best_of(plain))

    return compare


def test_overlay_versus_a_single_indexed_table(loaded):
    results = {}

    def run_pass(compare):

        # ---------------------------------------------------- point lookup by pk
        compare(
            "point lookup by pk",
            lambda: Person.objects.get(pk=HALF // 2),
            lambda: list(Person.objects.raw("SELECT * FROM bench_plain WHERE id = %s", [HALF // 2])),
        )

        # ------------------------------------- selective filter, the common case
        compare(
            "selective filter (age=42), first 50",
            lambda: list(Person.objects.filter(age=42).order_by("id")[:50]),
            lambda: list(Person.objects.raw("SELECT * FROM bench_plain WHERE age = 42 ORDER BY id LIMIT 50")),
        )

        # ------------------------------------------ keyset pagination, deep page
        # The cursor has to be negative. Under NEGATIVE_ID the view's ids run
        # -ROWS .. -(HALF+1) for the source rows and 1 .. HALF for the
        # materialised ones, so a *positive* cursor quietly selects the base
        # branch alone and times a query that never touches the source.
        compare(
            "keyset page (age=42, id > x)",
            lambda: list(Person.objects.filter(age=42, id__gt=-(ROWS + HALF) // 2).order_by("id")[:50]),
            lambda: list(
                Person.objects.raw("SELECT * FROM bench_plain WHERE age = 42 AND id > %s ORDER BY id LIMIT 50", [HALF])
            ),
        )

        # ------------------------------------------------- count under a filter
        compare(
            "count where age=42",
            lambda: Person.objects.filter(age=42).count(),
            lambda: list(Person.objects.raw("SELECT count(*) AS id FROM bench_plain WHERE age = 42")),
        )

        # ------------------------------ the two the TODO warns about, measured
        compare(
            "unfiltered ORDER BY id LIMIT 50",
            lambda: list(Person.objects.order_by("id")[:50]),
            lambda: list(Person.objects.raw("SELECT * FROM bench_plain ORDER BY id LIMIT 50")),
            "no filter to narrow the scan",
        )
        compare(
            "OFFSET 100000 LIMIT 50",
            lambda: list(Person.objects.order_by("id")[100_000:100_050]),
            lambda: list(Person.objects.raw("SELECT * FROM bench_plain ORDER BY id LIMIT 50 OFFSET 100000")),
            "use keyset instead",
        )

        # ------------------------------------------------------- a single write
        compare(
            "insert one row",
            lambda: Person.objects.create(first_name="w", age=1),
            lambda: sql("INSERT INTO bench_plain (id, first_name, age) SELECT max(id) + 1, 'w', 1 FROM bench_plain"),
            "view insert goes through the INSTEAD OF trigger",
        )

    run_pass(measure(results, "no_expr_index"))

    # The index the negation needs. `WHERE id = 100` on the view reaches the
    # source as `-source.id = 100`, which no plain index on source.id can
    # serve -- only an expression index on the negated column can.
    sql("CREATE INDEX IF NOT EXISTS bench_source_negid ON testapp_shared_personsource ((-id))")
    # And the composite the sort needs. `ORDER BY id` on the view is ascending
    # `-source.id`, so an (age, id) index on the source is in the wrong order to
    # feed a Merge Append -- it has to be on the negated column too.
    sql("CREATE INDEX IF NOT EXISTS bench_source_age_negid ON testapp_shared_personsource (age, (-id))")
    sql("ANALYZE testapp_shared_personsource")
    run_pass(measure(results, "with_expr_index"))

    width = max(len(label) for label in results)
    print(f"\n\n=== overlay view vs one indexed table, {ROWS:,} rows ===")
    print(f"{'query':<{width}}  {'plain':>8}  {'overlay':>9}  {'+expr idx':>10}  {'ratio':>6}  note")
    for label, row in results.items():
        plain, bare, indexed = row["plain"], row["no_expr_index"], row["with_expr_index"]
        print(
            f"{label:<{width}}  {plain:>7.2f}ms  {bare:>8.2f}ms  {indexed:>9.2f}ms  "
            f"{indexed / plain:>5.1f}x  {row['note']}"
        )

    print("\n=== plans, with the expression index in place ===")
    for label, query in [
        ("overlay, point lookup", "SELECT * FROM person_view WHERE id = -300000"),
        ("plain,   point lookup", "SELECT * FROM bench_plain WHERE id = 300000"),
        ("overlay, selective filter", 'SELECT * FROM person_view WHERE age = 42 ORDER BY "id" LIMIT 50'),
        ("plain,   selective filter", "SELECT * FROM bench_plain WHERE age = 42 ORDER BY id LIMIT 50"),
    ]:
        print(f"{label:<26} {plan(query)}")
