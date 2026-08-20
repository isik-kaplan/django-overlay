"""The query shapes that hurt most, ranked by measurement rather than by guess.

Not run by CI, and not run by the default suite. Run it deliberately:

    POSTGRES_USER=postgres uv run pytest tests/probe_worst_shapes.py -s -q \
        -o addopts="" --no-cov

Two ratios: 5% of rows materialised in the tenant's own table (the expected
deployment) and 50% (a tenant who has edited half the vendor's data).

Every shape is measured against `bench_plain`, an ordinary indexed table holding
the same rows -- what you would have written without an overlay -- so the
question each row answers is "what does the overlay cost me *here*", not "is
this query slow in general". Several of these are slow on a plain table too.

Each case also reports the rows it produced, because a shape that quietly
matches nothing would otherwise look like the fastest thing here.
"""

import os
import time

import pytest
from django.db import connection
from django.db.models import Avg, Count

from tests.testapp.models import Person, RemovableFkTest


pytestmark = pytest.mark.django_db(transaction=True)

ROWS = int(os.environ.get("OVERLAY_BENCH_ROWS", 400_000))
SHARES = [float(s) for s in os.environ.get("OVERLAY_BENCH_SHARES", "0.05,0.50").split(",")]
PAGE = 50
DELETED = 2_000


def sql(statement, *params):
    with connection.cursor() as cursor:
        cursor.execute(statement, params or None)


def best_of(fn, rounds=3):
    best, produced = None, None
    for _ in range(rounds):
        started = time.perf_counter()
        produced = fn()
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return best, produced


def reset():
    sql("SET CONSTRAINTS ALL IMMEDIATE")
    sql("TRUNCATE person, testapp_shared_personsource, testapp_removablefktest CASCADE")
    sql("SET CONSTRAINTS ALL DEFERRED")


def load(share):
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
    sql("INSERT INTO bench_plain SELECT g, 'n' || g, g %% 90 FROM generate_series(1, %s) g", ROWS)

    for table in ("person", "testapp_shared_personsource", "bench_plain"):
        sql(f"CREATE INDEX IF NOT EXISTS ws_{table}_age ON {table} (age)")
    sql("CREATE INDEX IF NOT EXISTS ws_source_negid ON testapp_shared_personsource ((-id))")
    sql("CREATE INDEX IF NOT EXISTS ws_source_age_negid ON testapp_shared_personsource (age, (-id))")
    for table in ("person", "testapp_shared_personsource", "bench_plain"):
        sql(f"ANALYZE {table}")
    return materialised


def raw(statement, *params):
    return lambda: list(Person.objects.raw(statement, list(params)))


def n(result):
    if isinstance(result, int):
        return result
    try:
        return len(result)
    except TypeError:
        return 1


def cases():
    """(group, shape, overlay, plain). The report sorts them by cost."""
    return [
        # ---------------------------------------------------------- unfiltered
        (
            "none",
            "order_by('id')[:50]",
            lambda: list(Person.objects.order_by("id")[:PAGE]),
            raw(f"SELECT * FROM bench_plain ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "none",
            "order_by('id')[100000:100050]",
            lambda: list(Person.objects.order_by("id")[100_000 : 100_000 + PAGE]),
            raw(f"SELECT * FROM bench_plain ORDER BY id LIMIT {PAGE} OFFSET 100000"),
        ),
        (
            "none",
            "order_by('first_name')[:50]  (unindexed column)",
            lambda: list(Person.objects.order_by("first_name")[:PAGE]),
            raw(f"SELECT * FROM bench_plain ORDER BY first_name LIMIT {PAGE}"),
        ),
        (
            "none",
            "count()",
            lambda: Person.objects.count(),
            raw("SELECT count(*) AS id FROM bench_plain"),
        ),
        (
            "none",
            "distinct().order_by('id')[:50]",
            lambda: list(Person.objects.distinct().order_by("id")[:PAGE]),
            raw(f"SELECT DISTINCT * FROM bench_plain ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "none",
            "values('age').annotate(Count('id'))",
            lambda: list(Person.objects.values("age").annotate(c=Count("id"))),
            raw("SELECT age AS id, count(*) AS c FROM bench_plain GROUP BY age"),
        ),
        (
            "none",
            "aggregate(Avg('age'))",
            lambda: Person.objects.aggregate(a=Avg("age")),
            raw("SELECT 1 AS id, avg(age) AS a FROM bench_plain"),
        ),
        (
            "none",
            "last()  (order_by('-id').first())",
            lambda: [Person.objects.order_by("-id").first()],
            raw("SELECT * FROM bench_plain ORDER BY id DESC LIMIT 1"),
        ),
        (
            "none",
            "exists()",
            lambda: [Person.objects.exists()],
            raw("SELECT * FROM bench_plain LIMIT 1"),
        ),
        # ------------------------------------------- filters that don't filter
        (
            "fake",
            "filter(age__gte=0).order_by('id')[:50]",
            lambda: list(Person.objects.filter(age__gte=0).order_by("id")[:PAGE]),
            raw(f"SELECT * FROM bench_plain WHERE age >= 0 ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "fake",
            "exclude(age=42).order_by('id')[:50]",
            lambda: list(Person.objects.exclude(age=42).order_by("id")[:PAGE]),
            raw(f"SELECT * FROM bench_plain WHERE NOT (age = 42) ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "fake",
            "filter(first_name__icontains='n1234').order_by('id')[:50]",
            lambda: list(Person.objects.filter(first_name__icontains="n1234").order_by("id")[:PAGE]),
            raw(f"SELECT * FROM bench_plain WHERE first_name ILIKE '%%n1234%%' ORDER BY id LIMIT {PAGE}"),
        ),
        # ------------------------------------------------ genuinely selective
        (
            "age=42",
            "filter(age=42).order_by('first_name')[:50]  (unindexed sort)",
            lambda: list(Person.objects.filter(age=42).order_by("first_name")[:PAGE]),
            raw(f"SELECT * FROM bench_plain WHERE age = 42 ORDER BY first_name LIMIT {PAGE}"),
        ),
        (
            "age=42",
            "filter(age=42).order_by('id')[3000:3050]",
            lambda: list(Person.objects.filter(age=42).order_by("id")[3000 : 3000 + PAGE]),
            raw(f"SELECT * FROM bench_plain WHERE age = 42 ORDER BY id LIMIT {PAGE} OFFSET 3000"),
        ),
        (
            "age=42",
            "filter(age=42).order_by('id')[:50]",
            lambda: list(Person.objects.filter(age=42).order_by("id")[:PAGE]),
            raw(f"SELECT * FROM bench_plain WHERE age = 42 ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "age=42",
            "filter(age=42).count()",
            lambda: Person.objects.filter(age=42).count(),
            raw("SELECT count(*) AS id FROM bench_plain WHERE age = 42"),
        ),
        (
            "age=42",
            "filter(age=42).aggregate(Avg('age'))",
            lambda: Person.objects.filter(age=42).aggregate(a=Avg("age")),
            raw("SELECT 1 AS id, avg(age) AS a FROM bench_plain WHERE age = 42"),
        ),
    ]


def measure_delete(measured, counts, share):
    """Destructive, so it runs last and is timed once rather than best-of-3.

    The baseline is an ORM delete of the same number of rows from
    RemovableFkTest -- a plain model with no reverse relations -- so both sides
    pay for Django's collector and only one pays for the INSTEAD OF trigger.
    """
    RemovableFkTest.objects.bulk_create([RemovableFkTest(label="x") for _ in range(DELETED)])
    plain_ids = list(RemovableFkTest.objects.values_list("pk", flat=True)[:DELETED])
    started = time.perf_counter()
    RemovableFkTest.objects.filter(pk__in=plain_ids).delete()
    plain_ms = (time.perf_counter() - started) * 1000

    ids = list(Person.objects.filter(age=42).values_list("pk", flat=True)[:DELETED])
    started = time.perf_counter()
    Person.objects.filter(pk__in=ids).delete()
    overlay_ms = (time.perf_counter() - started) * 1000

    key = ("write", f"delete() of {DELETED:,} rows")
    row = measured.setdefault(key, {})
    row[share] = overlay_ms
    row.setdefault("plain", plain_ms)
    counts[key] = len(ids)


def test_worst_query_shapes():
    measured, counts = {}, {}
    for share in SHARES:
        load(share)
        for group, shape, overlay, plain in cases():
            elapsed, produced = best_of(overlay)
            key = (group, shape)
            row = measured.setdefault(key, {})
            row[share] = elapsed
            row.setdefault("plain", best_of(plain)[0])
            counts[key] = n(produced)
        measure_delete(measured, counts, share)

    worst = max(SHARES)
    ranked = sorted(measured.items(), key=lambda kv: kv[1][worst], reverse=True)

    width = max(len(shape) for _, shape in measured)
    print(f"\n\n=== worst query shapes, {ROWS:,} rows, ranked by cost at {worst:.0%} materialised ===")
    header = "  ".join(f"{s:>9.0%}" for s in SHARES)
    print(f"{'#':>2}  {'filter':<7}  {'shape':<{width}}  {'plain':>9}  {header}  {'rows':>7}")
    for rank, ((group, shape), times) in enumerate(ranked, 1):
        cells = "  ".join(f"{times[s]:>7.1f}ms" for s in SHARES)
        print(
            f"{rank:>2}  {group:<7}  {shape:<{width}}  {times['plain']:>7.1f}ms  {cells}  {counts[(group, shape)]:>7,}"
        )

    sql("DROP TABLE IF EXISTS bench_plain")
    reset()
