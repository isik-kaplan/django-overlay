"""What a join against the view costs.

Not run by CI, and not run by the default suite. Run it deliberately:

    POSTGRES_USER=postgres uv run pytest tests/probe_join_scale.py -s -q \
        -o addopts="" --no-cov

`tests/test_joins.py` proves join traversal is *correct* across every shape.
This asks what it costs, because a join against an overlay model is a join
against a view, and each side of it drags its own anti-join along.

The comparison is three ordinary tables -- bench_person, bench_address,
bench_link -- holding the same rows with the same indexes, joined the same way.
Every case asserts it matched rows before it is timed.
"""

import os
import time

import pytest
from django.db import connection

from tests.testapp.models import Address, Person


pytestmark = pytest.mark.django_db(transaction=True)

ROWS = int(os.environ.get("OVERLAY_BENCH_ROWS", 200_000))
SHARES = [float(s) for s in os.environ.get("OVERLAY_BENCH_SHARES", "0.05,0.50").split(",")]
# 500 distinct cities over ROWS addresses -- selective enough to be a realistic
# filter, big enough that the answer isn't a single row.
CITIES = 500


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
    sql(
        "TRUNCATE person, address, testapp_shared_personsource, "
        "testapp_shared_addresssource, testapp_personaddressthrough CASCADE"
    )
    sql("SET CONSTRAINTS ALL DEFERRED")


def load(share):
    """`share` of both people and addresses materialised, the rest in source.

    Person g is linked to address g, so the join matches one-to-one and the
    row counts are the same on the overlay and plain sides.
    """
    materialised = int(ROWS * share)
    reset()
    for table, source, extra in (
        ("person", "testapp_shared_personsource", "first_name, age"),
        ("address", "testapp_shared_addresssource", "street, city"),
    ):
        values = "'n' || g, g %% 90" if table == "person" else f"'st' || g, 'city' || (g %% {CITIES})"
        if materialised:
            sql(
                f"INSERT INTO {table} (id, {extra}, _overlay_deleted) "
                f"SELECT g, {values}, FALSE FROM generate_series(1, %s) g",
                materialised,
            )
        sql(
            f"INSERT INTO {source} (id, {extra}) SELECT g, {values} FROM generate_series(%s, %s) g",
            materialised + 1,
            ROWS,
        )

    # The through table is a plain table either way, and holds view-space ids:
    # positive for materialised rows, negative for source-backed ones.
    sql(
        "INSERT INTO testapp_personaddressthrough (id, person_id, address_id, label) "
        "SELECT g, CASE WHEN g <= %s THEN g ELSE -g END, CASE WHEN g <= %s THEN g ELSE -g END, 'home' "
        "FROM generate_series(1, %s) g",
        materialised,
        materialised,
        ROWS,
    )

    for table in ("bench_link", "bench_person", "bench_address"):
        sql(f"DROP TABLE IF EXISTS {table}")
    sql("CREATE TABLE bench_person (id bigint PRIMARY KEY, first_name varchar(100) NOT NULL, age integer)")
    sql("CREATE TABLE bench_address (id bigint PRIMARY KEY, street varchar(100) NOT NULL, city varchar(100) NOT NULL)")
    sql("CREATE TABLE bench_link (id bigint PRIMARY KEY, person_id bigint NOT NULL, address_id bigint NOT NULL)")
    sql("INSERT INTO bench_person SELECT g, 'n' || g, g %% 90 FROM generate_series(1, %s) g", ROWS)
    sql(
        f"INSERT INTO bench_address SELECT g, 'st' || g, 'city' || (g %% {CITIES}) FROM generate_series(1, %s) g",
        ROWS,
    )
    sql("INSERT INTO bench_link SELECT g, g, g FROM generate_series(1, %s) g", ROWS)

    sql("CREATE INDEX IF NOT EXISTS jb_address_city ON address (city)")
    sql("CREATE INDEX IF NOT EXISTS jb_source_city ON testapp_shared_addresssource (city)")
    sql("CREATE INDEX IF NOT EXISTS jb_bench_city ON bench_address (city)")
    sql("CREATE INDEX IF NOT EXISTS jb_link_person ON testapp_personaddressthrough (person_id)")
    sql("CREATE INDEX IF NOT EXISTS jb_link_address ON testapp_personaddressthrough (address_id)")
    sql("CREATE INDEX IF NOT EXISTS jb_bl_person ON bench_link (person_id)")
    sql("CREATE INDEX IF NOT EXISTS jb_bl_address ON bench_link (address_id)")
    # The expression indexes the negated id space needs on both source tables.
    sql("CREATE INDEX IF NOT EXISTS jb_psource_negid ON testapp_shared_personsource ((-id))")
    sql("CREATE INDEX IF NOT EXISTS jb_asource_negid ON testapp_shared_addresssource ((-id))")

    for table in (
        "person",
        "address",
        "testapp_shared_personsource",
        "testapp_shared_addresssource",
        "testapp_personaddressthrough",
        "bench_person",
        "bench_address",
        "bench_link",
    ):
        sql(f"ANALYZE {table}")


PLAIN_JOIN = (
    "SELECT p.* FROM bench_person p "
    "JOIN bench_link l ON l.person_id = p.id "
    "JOIN bench_address a ON a.id = l.address_id "
    "WHERE a.city = 'city42'"
)


def cases():
    # Each lambda has to build its own queryset. Evaluating one caches its rows
    # on the instance, so a shared queryset would be timed once and then
    # measured six more times against its own result cache -- which reads as
    # 0.00ms and looks like a spectacular win.
    def joined():
        return Person.objects.filter(addresses__city="city42")

    return [
        (
            "join filter, fetch matches",
            lambda: list(joined()),
            lambda: list(Person.objects.raw(PLAIN_JOIN)),
        ),
        (
            "join filter, first 50 ordered",
            lambda: list(joined().order_by("id")[:50]),
            lambda: list(Person.objects.raw(PLAIN_JOIN + " ORDER BY p.id LIMIT 50")),
        ),
        (
            "join filter, count",
            lambda: [joined().count()],
            lambda: list(Person.objects.raw(PLAIN_JOIN.replace("SELECT p.*", "SELECT count(*) AS id"))),
        ),
        (
            "reverse join, addresses by person",
            lambda: list(Address.objects.filter(people__first_name="n42")),
            lambda: list(
                Person.objects.raw(
                    "SELECT a.* FROM bench_address a "
                    "JOIN bench_link l ON l.address_id = a.id "
                    "JOIN bench_person p ON p.id = l.person_id "
                    "WHERE p.first_name = 'n42'"
                )
            ),
        ),
    ]


def test_what_a_join_costs():
    measured = []
    for share in SHARES:
        load(share)
        for label, overlay, plain in cases():
            assert overlay(), f"{label} at {share:.0%} matched nothing"
            assert plain(), f"{label} at {share:.0%} matched nothing on the plain tables"
            measured.append((share, label, best_of(plain), best_of(overlay)))

    width = max(len(row[1]) for row in measured)
    print(f"\n\n=== joins against the view, {ROWS:,} rows each side ===")
    print(f"{'shown':>6}  {'query':<{width}}  {'plain':>9}  {'overlay':>9}  {'ratio':>6}")
    for share, label, plain, overlay in measured:
        print(f"{share:>5.0%}  {label:<{width}}  {plain:>7.2f}ms  {overlay:>7.2f}ms  {overlay / plain:>5.1f}x")

    for table in ("bench_link", "bench_person", "bench_address"):
        sql(f"DROP TABLE IF EXISTS {table}")
    reset()
