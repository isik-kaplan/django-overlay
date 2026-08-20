"""How filtering, ordering and pagination scale with how much is materialised.

Not run by CI, and not run by the default suite. Run it deliberately:

    POSTGRES_USER=postgres uv run pytest tests/probe_ratio.py -s -q \
        -o addopts="" --no-cov

    OVERLAY_BENCH_SHARES=0.05,0.5 POSTGRES_USER=postgres uv run pytest ...

The one number that governs everything here is the share of rows that live in
the tenant's own table rather than showing through from the vendor's. Every
query pays for an anti-join against that table, so the question this probe
answers is: how much does that share cost you?

DRF isn't a dependency, so the pagination cases issue the query *shapes* its
three built-in paginators produce, which is what the database sees either way:

    PageNumberPagination   -> qs.count()  +  qs.order_by(pk)[offset:offset+50]
    LimitOffsetPagination  -> the same two queries
    CursorPagination       -> qs.order_by(pk).filter(pk__gt=cursor)[:51]

Two traps this probe is built to avoid, both of which produce flatteringly fast
numbers rather than obvious failures:

  * Under NEGATIVE_ID the view's id space runs -ROWS .. -(M+1) for vendor rows
    and 1 .. M for materialised ones. A *positive* cursor silently selects the
    materialised branch alone; at a small share it selects nothing at all. Every
    case therefore asserts it matched rows before it is timed.
  * Evaluating a QuerySet caches its rows on the instance. Each case builds a
    fresh one per round, or rounds 2..n would be timed against that cache.
"""

import os
import time

import pytest
from django.db import connection

from tests.testapp.models import Person


pytestmark = pytest.mark.django_db(transaction=True)

ROWS = int(os.environ.get("OVERLAY_BENCH_ROWS", 400_000))
SHARES = [float(s) for s in os.environ.get("OVERLAY_BENCH_SHARES", "0.05,0.50").split(",")]
PAGE = 50
DEEP = 1950  # inside the ~4,400 rows age=42 matches, so the page isn't empty


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


def reset():
    sql("SET CONSTRAINTS ALL IMMEDIATE")
    sql("TRUNCATE person, testapp_shared_personsource CASCADE")
    sql("SET CONSTRAINTS ALL DEFERRED")


def load(share):
    """`share` of the rows materialised in the tenant's table, the rest showing
    through from the vendor's."""
    materialised = int(ROWS * share)
    reset()
    if materialised:
        sql(
            "INSERT INTO person (id, first_name, age, _overlay_deleted) "
            "SELECT g, 'n' || g, g %% 90, FALSE FROM generate_series(1, %s) g",
            materialised,
        )
    sql(
        "INSERT INTO testapp_shared_personsource (id, first_name, age) "
        "SELECT g, 'n' || g, g %% 90 FROM generate_series(%s, %s) g",
        materialised + 1,
        ROWS,
    )
    sql("DROP TABLE IF EXISTS bench_plain")
    sql("CREATE TABLE bench_plain (id bigint PRIMARY KEY, first_name varchar(100) NOT NULL, age integer)")
    sql("INSERT INTO bench_plain (id, first_name, age) SELECT g, 'n' || g, g %% 90 FROM generate_series(1, %s) g", ROWS)

    for table in ("person", "testapp_shared_personsource", "bench_plain"):
        sql(f"CREATE INDEX IF NOT EXISTS rt_{table}_age ON {table} (age)")
    # The indexes the negated id space needs; see docs/operations/PERFORMANCE.md.
    sql("CREATE INDEX IF NOT EXISTS rt_source_negid ON testapp_shared_personsource ((-id))")
    sql("CREATE INDEX IF NOT EXISTS rt_source_age_negid ON testapp_shared_personsource (age, (-id))")
    for table in ("person", "testapp_shared_personsource", "bench_plain"):
        sql(f"ANALYZE {table}")
    return materialised


def raw(statement, *params):
    return lambda: list(Person.objects.raw(statement, list(params)))


def rows_of(result):
    """Paginator shapes return (count, page); everything else returns rows."""
    return result[-1] if isinstance(result, tuple) else result


def cases(materialised):
    def every():
        return Person.objects.all()

    def filtered():
        return Person.objects.filter(age=42)

    # A row still in the vendor's table, and one already materialised. They take
    # different paths: only the first has to be found through the anti-join.
    vendor_pk = -(materialised + (ROWS - materialised) // 2)
    base_pk = max(1, materialised // 2)
    # Halfway through the vendor branch, so a cursor page spans both branches.
    cursor = -((materialised + ROWS) // 2)

    return [
        # ------------------------------------------------------------ filtering
        (
            "filter",
            "point lookup, vendor row",
            lambda: [Person.objects.get(pk=vendor_pk)],
            raw("SELECT * FROM bench_plain WHERE id = %s", -vendor_pk),
        ),
        (
            "filter",
            "point lookup, materialised row",
            lambda: [Person.objects.get(pk=base_pk)],
            raw("SELECT * FROM bench_plain WHERE id = %s", base_pk),
        ),
        (
            "filter",
            "age=42, fetch all matches",
            lambda: list(filtered()),
            raw("SELECT * FROM bench_plain WHERE age = 42"),
        ),
        (
            "filter",
            "age=42, count",
            lambda: [filtered().count()],
            raw("SELECT count(*) AS id FROM bench_plain WHERE age = 42"),
        ),
        (
            "filter",
            "count everything",
            lambda: [every().count()],
            raw("SELECT count(*) AS id FROM bench_plain"),
        ),
        # ------------------------------------------------------------- ordering
        (
            "order",
            "age=42, order by id, first 50",
            lambda: list(filtered().order_by("id")[:PAGE]),
            raw(f"SELECT * FROM bench_plain WHERE age = 42 ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "order",
            "unfiltered, order by id, first 50",
            lambda: list(every().order_by("id")[:PAGE]),
            raw(f"SELECT * FROM bench_plain ORDER BY id LIMIT {PAGE}"),
        ),
        # ----------------------------------------------------------- pagination
        (
            "paginate",
            "Cursor, age=42",
            lambda: list(filtered().filter(id__gt=cursor).order_by("id")[: PAGE + 1]),
            raw(f"SELECT * FROM bench_plain WHERE age = 42 AND id > %s ORDER BY id LIMIT {PAGE + 1}", ROWS // 2),
        ),
        (
            "paginate",
            "PageNumber, age=42, page 1",
            lambda: (filtered().count(), list(filtered().order_by("id")[:PAGE])),
            lambda: (
                list(Person.objects.raw("SELECT count(*) AS id FROM bench_plain WHERE age = 42")),
                list(Person.objects.raw(f"SELECT * FROM bench_plain WHERE age = 42 ORDER BY id LIMIT {PAGE}")),
            ),
        ),
        (
            "paginate",
            "PageNumber, age=42, deep page",
            lambda: (filtered().count(), list(filtered().order_by("id")[DEEP : DEEP + PAGE])),
            lambda: (
                list(Person.objects.raw("SELECT count(*) AS id FROM bench_plain WHERE age = 42")),
                list(
                    Person.objects.raw(
                        f"SELECT * FROM bench_plain WHERE age = 42 ORDER BY id LIMIT {PAGE} OFFSET {DEEP}"
                    )
                ),
            ),
        ),
        (
            "paginate",
            "Cursor, no filter",
            lambda: list(every().filter(id__gt=cursor).order_by("id")[: PAGE + 1]),
            raw(f"SELECT * FROM bench_plain WHERE id > %s ORDER BY id LIMIT {PAGE + 1}", ROWS // 2),
        ),
        (
            "paginate",
            "PageNumber, no filter, page 1",
            lambda: (every().count(), list(every().order_by("id")[:PAGE])),
            lambda: (
                list(Person.objects.raw("SELECT count(*) AS id FROM bench_plain")),
                list(Person.objects.raw(f"SELECT * FROM bench_plain ORDER BY id LIMIT {PAGE}")),
            ),
        ),
        (
            "paginate",
            "PageNumber, no filter, page 200",
            lambda: (every().count(), list(every().order_by("id")[9950 : 9950 + PAGE])),
            lambda: (
                list(Person.objects.raw("SELECT count(*) AS id FROM bench_plain")),
                list(Person.objects.raw(f"SELECT * FROM bench_plain ORDER BY id LIMIT {PAGE} OFFSET 9950")),
            ),
        ),
    ]


def test_how_the_materialised_share_costs():
    measured = {}
    for share in SHARES:
        materialised = load(share)
        for group, label, overlay, plain in cases(materialised):
            assert rows_of(overlay()), f"{label} at {share:.0%} matched nothing -- it would time an empty query"
            row = measured.setdefault((group, label), {})
            row[share] = best_of(overlay)
            row.setdefault("plain", best_of(plain))

    width = max(len(label) for _, label in measured)
    print(f"\n\n=== {ROWS:,} rows, by share materialised in the tenant's table ===")
    header = "  ".join(f"{s:>8.0%}" for s in SHARES)
    print(f"{'query':<{width}}  {'plain':>9}  {header}")
    last = None
    for (group, label), row in measured.items():
        if group != last:
            print(f"-- {group}")
            last = group
        times = "  ".join(f"{row[s]:>6.2f}ms" for s in SHARES)
        print(f"{label:<{width}}  {row['plain']:>7.2f}ms  {times}")

    sql("DROP TABLE IF EXISTS bench_plain")
    reset()
