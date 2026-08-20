"""How much does the INSTEAD OF trigger cost, as a function of batch size?

Compares the same write against the overlay view and against an identically
shaped plain table (the model's own hidden base table, which has the same
columns and indexes but no triggers). Run with:

    POSTGRES_USER=postgres uv run pytest tests/probe_write_cost.py \
        -s -q -p no:cacheprovider --no-cov
"""

import time

import pytest
from django.db import connection

from tests.testapp.models import Person
from tests.testapp_shared.models import PersonSource


pytestmark = pytest.mark.django_db

SIZES = (1, 10, 50, 500, 5000)
REPEATS = 5


def timed(fn):
    """Best of REPEATS — the minimum is the least noisy estimate here."""
    best = None
    for _ in range(REPEATS):
        started = time.perf_counter()
        fn()
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    return best * 1000


def reset(cursor):
    # The delete-side FK trigger is deferred, and Postgres refuses to TRUNCATE
    # a table with pending trigger events. Flushing them first is what an
    # application would have to do too.
    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    cursor.execute("TRUNCATE person, testapp_shared_personsource RESTART IDENTITY CASCADE")
    cursor.execute("SET CONSTRAINTS ALL DEFERRED")


def seed_base(cursor, n):
    cursor.execute(
        "INSERT INTO person (first_name, age, _overlay_deleted) "
        "SELECT 'seed' || g, g, FALSE FROM generate_series(1, %s) g",
        [n],
    )


def seed_source(cursor, n):
    cursor.execute(
        "INSERT INTO testapp_shared_personsource (first_name, age) SELECT 'src' || g, g FROM generate_series(1, %s) g",
        [n],
    )


def test_write_cost_by_batch_size():
    rows = []
    with connection.cursor() as cursor:
        for n in SIZES:
            # INSERT: through the view (trigger) vs straight into the table.
            def insert_view(n=n, cursor=cursor):
                reset(cursor)
                cursor.execute(
                    "INSERT INTO person_view (first_name, age) SELECT 'v' || g, g FROM generate_series(1, %s) g",
                    [n],
                )

            def insert_table(n=n, cursor=cursor):
                reset(cursor)
                cursor.execute(
                    "INSERT INTO person (first_name, age, _overlay_deleted) "
                    "SELECT 't' || g, g, FALSE FROM generate_series(1, %s) g",
                    [n],
                )

            # UPDATE of rows that already live in the base table.
            def update_view(n=n, cursor=cursor):
                cursor.execute("UPDATE person_view SET age = age + 1 WHERE id > 0")

            def update_table(n=n, cursor=cursor):
                cursor.execute("UPDATE person SET age = age + 1")

            # UPDATE of source-only rows: the copy-on-write path, the most
            # expensive thing the update trigger does.
            def update_materializing(n=n, cursor=cursor):
                reset(cursor)
                seed_source(cursor, n)
                cursor.execute("UPDATE person_view SET age = age + 1 WHERE id < 0")

            insert_v = timed(insert_view)
            insert_t = timed(insert_table)

            reset(cursor)
            seed_base(cursor, n)
            update_v = timed(update_view)

            reset(cursor)
            seed_base(cursor, n)
            update_t = timed(update_table)

            materialize = timed(update_materializing)

            rows.append((n, insert_t, insert_v, update_t, update_v, materialize))
        reset(cursor)

    print(f"\n\nAll times are best-of-{REPEATS}, milliseconds.\n")
    header = (
        f"{'rows':>6}  {'INSERT table':>13}  {'INSERT view':>12}  {'x':>5}   "
        f"{'UPDATE table':>13}  {'UPDATE view':>12}  {'x':>5}   {'materialize':>12}"
    )
    print(header)
    print("-" * len(header))
    for n, it, iv, ut, uv, mat in rows:
        print(
            f"{n:>6}  {it:>13.2f}  {iv:>12.2f}  {iv / it:>5.1f}   "
            f"{ut:>13.2f}  {uv:>12.2f}  {uv / ut:>5.1f}   {mat:>12.2f}"
        )
    print("\nper-row overhead (view minus table, microseconds):")
    for n, it, iv, ut, uv, _mat in rows:
        print(f"{n:>6}  insert {1000 * (iv - it) / n:>8.1f}   update {1000 * (uv - ut) / n:>8.1f}")


def test_orm_level_cost_for_a_realistic_batch():
    """What the numbers above mean through the ORM, at the size that actually
    shows up in a request."""
    with connection.cursor() as cursor:
        reset(cursor)

    def bulk_create_50():
        Person.objects.bulk_create([Person(first_name=f"p{i}", age=i) for i in range(50)])

    def bulk_update_50():
        people = list(Person.objects.all()[:50])
        for person in people:
            person.age += 1
        Person.objects.bulk_update(people, ["age"])

    created = timed(bulk_create_50)
    updated = timed(bulk_update_50)
    print(f"\nORM bulk_create(50 objs): {created:.2f} ms")
    print(f"ORM bulk_update(50 objs): {updated:.2f} ms")

    with connection.cursor() as cursor:
        reset(cursor)
        seed_source(cursor, 50)
    qs_update = timed(lambda: Person.objects.filter(id__lt=0).update(age=1))
    print(f"ORM .update() materializing 50 source rows: {qs_update:.2f} ms")

    with connection.cursor() as cursor:
        reset(cursor)
    PersonSource.objects.all().delete()
