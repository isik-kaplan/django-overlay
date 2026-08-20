"""The standing benchmark format: every shape, four questions.

For each query shape, measured on an indexed column *and* an unindexed one:

  1. **plain**      wall time against a plain table holding identical rows
  2. **overlay**    wall time through the view
  3. **ratio**      overlay / plain, e.g. `x124.31`
  4. **O(N)**       does total row count matter? exponent k where doubling N
                    multiplies time by 2^k. 0.0 = flat, 1.0 = linear.
  5. **O(share)**   does the base/source split matter? factor between a 5%
                    materialised view and a 50% one at the *same* total size.

The two complexity columns are measured, not derived: three separate loads,

    A = (N,   50% materialised)
    B = (2N,  50% materialised)     -> O(N)     = log2(tB / tA)
    C = (N,    5% materialised)     -> O(share) = tA / tC

so N and the share vary independently. The view always exposes exactly N rows;
`share` only moves how many of them come from the base table rather than the
source. That is the whole point — a naive loader changes both at once and the
two effects become impossible to separate.

    POSTGRES_USER=postgres uv run pytest tests/probe_complexity.py -s -q \
        -o addopts="" --no-cov

    OVERLAY_COMPLEXITY_ROWS=1000000 POSTGRES_USER=postgres uv run pytest ...

Default N is deliberately modest so the three loads finish in a couple of
minutes. Plan shapes can change with size, so treat a small run as indicative
and re-run at production scale before quoting anything.
"""

import math
import os
import time

import pytest
from django.db import OperationalError, connection
from django.db.models import Avg

from tests.testapp.models import WideCustomerU7, WideOrderU7


pytestmark = pytest.mark.django_db(transaction=True)

ROWS = int(os.environ.get("OVERLAY_COMPLEXITY_ROWS", 400_000))
TIMEOUT_MS = int(os.environ.get("OVERLAY_WIDE_TIMEOUT_MS", 30_000))
PAGE = 50

CUSTOMER_BASE = "widecustomer_u7"
CUSTOMER_SOURCE = "testapp_shared_widecustomeru7source"
ORDER_BASE = "wideorder_u7"
ORDER_SOURCE = "testapp_shared_wideorderu7source"
MIRROR = "cx_customer"
ORDER_MIRROR = "cx_order"

UUID_EXPR = (
    "(lpad(to_hex({g}), 12, '0') || '7' || substr(md5({g}::text), 1, 3)"
    " || '8' || substr(md5({g}::text || 'x'), 1, 15))::uuid"
)


def u(g):
    return UUID_EXPR.format(g=g)


def sql(statement, *params):
    with connection.cursor() as cursor:
        cursor.execute(statement, params or None)


def scalar(statement):
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return cursor.fetchone()[0]


def best_of(fn, rounds=3):
    best, produced = None, None
    for _ in range(rounds):
        sql(f"SET statement_timeout = {TIMEOUT_MS}")
        started = time.perf_counter()
        try:
            produced = fn()
        except OperationalError:
            sql("SET statement_timeout = 0")
            return None, None
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
        if best > 1500:
            break
    sql("SET statement_timeout = 0")
    return best, produced


def rows_of(result):
    if isinstance(result, int):
        return result
    try:
        return len(result)
    except TypeError:
        return 1


CUSTOMER_COLUMNS = "first_name, last_name, email, age, city, postcode, status, score, registered_on, notes, region_id"
CUSTOMER_VALUES = (
    "'first' || g, 'last' || (g %% 5000), 'e' || g || '@example.com', g %% 80, "
    "'city' || (g %% 1000), 'pc' || (g %% 1000), "
    "(ARRAY['active','lapsed','pending','closed'])[1 + g %% 4], g %% 1000, "
    "DATE '2015-01-01' + (g %% 3000), '', 1 + g %% 200"
)
ORDER_COLUMNS = "reference, status, total_cents, placed_on, channel, currency, comment, customer_id"


def load(view_rows: int, share: float):
    """Exactly `view_rows` rows visible through the view, of which `share` are
    materialised in the base table.

    Every base row is an *override* of a source row, so the view's row count is
    the source's row count regardless of the share — which is what lets N and
    the share be varied independently."""
    materialised = int(view_rows * share)

    sql("SET CONSTRAINTS ALL IMMEDIATE")
    sql(f"TRUNCATE {CUSTOMER_BASE}, {CUSTOMER_SOURCE}, {ORDER_BASE}, {ORDER_SOURCE} CASCADE")
    sql("SET CONSTRAINTS ALL DEFERRED")
    for mirror in (MIRROR, ORDER_MIRROR):
        sql(f"DROP TABLE IF EXISTS {mirror}")
    for table in (CUSTOMER_BASE, ORDER_BASE):
        sql(f"ALTER TABLE {table} DISABLE TRIGGER USER")

    if scalar("SELECT count(*) FROM testapp_wideregion") == 0:
        sql(
            "INSERT INTO testapp_wideregion (id, name, country) "
            "SELECT g, 'region' || g, 'GB' FROM generate_series(1, 200) g"
        )

    sql(
        f"INSERT INTO {CUSTOMER_SOURCE} (id, {CUSTOMER_COLUMNS}) "
        f"SELECT {u('g')}, {CUSTOMER_VALUES} FROM generate_series(1, %s) g",
        view_rows,
    )
    sql(
        f"INSERT INTO {CUSTOMER_BASE} (id, {CUSTOMER_COLUMNS}, _overlay_deleted) "
        f"SELECT {u('g')}, {CUSTOMER_VALUES}, FALSE FROM generate_series(1, %s) g",
        materialised,
    )

    order_rows = view_rows // 4
    order_values = (
        "'REF' || g, (ARRAY['new','paid','shipped'])[1 + g %% 3], 100 + g %% 500000, "
        f"DATE '2020-01-01' + (g %% 1500), 'web', 'GBP', '', {u(f'(1 + g %% {view_rows})')}"
    )
    sql(
        f"INSERT INTO {ORDER_SOURCE} (id, {ORDER_COLUMNS}) "
        f"SELECT {u('g')}, {order_values} FROM generate_series(1, %s) g",
        order_rows,
    )
    sql(
        f"INSERT INTO {ORDER_BASE} (id, {ORDER_COLUMNS}, _overlay_deleted) "
        f"SELECT {u('g')}, {order_values}, FALSE FROM generate_series(1, %s) g",
        int(order_rows * share),
    )

    for table in (CUSTOMER_BASE, ORDER_BASE):
        sql(f"ALTER TABLE {table} ENABLE TRIGGER USER")

    # The plain comparison, built from the view so its contents are identical
    # by construction, carrying the same indexes.
    sql(f"CREATE TABLE {MIRROR} AS SELECT * FROM widecustomer_u7_view")
    sql(f"ALTER TABLE {MIRROR} ADD PRIMARY KEY (id)")
    for column in ("last_name", "city", "status", "score", "age", "region_id"):
        sql(f"CREATE INDEX cx_customer_{column} ON {MIRROR} ({column})")
    sql(f"CREATE TABLE {ORDER_MIRROR} AS SELECT * FROM wideorder_u7_view")
    sql(f"ALTER TABLE {ORDER_MIRROR} ADD PRIMARY KEY (id)")
    for column in ("status", "channel", "customer_id"):
        sql(f"CREATE INDEX cx_order_{column} ON {ORDER_MIRROR} ({column})")

    for table in (CUSTOMER_BASE, CUSTOMER_SOURCE, ORDER_BASE, ORDER_SOURCE, MIRROR, ORDER_MIRROR):
        sql(f"ANALYZE {table}")


def raw(model, statement):
    return lambda: list(model.objects.raw(statement))


def shapes(pk):
    """(group, label, indexed, overlay, plain).

    `indexed` says whether the *filter column* is indexed, so the report can be
    read down that axis — it is the single biggest lever and it is easy to
    forget which column is which.

    `pk` is resolved by the caller, before timing starts. Looking it up inside
    the lambda would put an extra round trip on the overlay side that the plain
    baseline does not pay, and at 0.2ms a round trip is most of the result."""
    C = WideCustomerU7.objects
    return [
        (
            "lookup",
            "get(pk=…)",
            None,
            lambda: [C.get(pk=pk)],
            raw(WideCustomerU7, f"SELECT * FROM {MIRROR} WHERE id = '{pk}'"),
        ),
        (
            "filter",
            "filter(city)[:50]",
            True,
            lambda: list(C.filter(city="city42")[:PAGE]),
            raw(WideCustomerU7, f"SELECT * FROM {MIRROR} WHERE city = 'city42' LIMIT {PAGE}"),
        ),
        (
            "filter",
            "filter(postcode)[:50]",
            False,
            lambda: list(C.filter(postcode="pc42")[:PAGE]),
            raw(WideCustomerU7, f"SELECT * FROM {MIRROR} WHERE postcode = 'pc42' LIMIT {PAGE}"),
        ),
        (
            "filter",
            "filter(city).count()",
            True,
            lambda: C.filter(city="city42").count(),
            raw(WideCustomerU7, f"SELECT count(*) AS id FROM {MIRROR} WHERE city = 'city42'"),
        ),
        (
            "filter",
            "filter(postcode).count()",
            False,
            lambda: C.filter(postcode="pc42").count(),
            raw(WideCustomerU7, f"SELECT count(*) AS id FROM {MIRROR} WHERE postcode = 'pc42'"),
        ),
        (
            "filter",
            "count() everything",
            None,
            lambda: C.count(),
            raw(WideCustomerU7, f"SELECT count(*) AS id FROM {MIRROR}"),
        ),
        (
            "order",
            "filter(city).order_by('id')[:50]",
            True,
            lambda: list(C.filter(city="city42").order_by("id")[:PAGE]),
            raw(WideCustomerU7, f"SELECT * FROM {MIRROR} WHERE city = 'city42' ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "order",
            "filter(postcode).order_by('id')[:50]",
            False,
            lambda: list(C.filter(postcode="pc42").order_by("id")[:PAGE]),
            raw(WideCustomerU7, f"SELECT * FROM {MIRROR} WHERE postcode = 'pc42' ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "order",
            "order_by('id')[:50]  no filter",
            None,
            lambda: list(C.order_by("id")[:PAGE]),
            raw(WideCustomerU7, f"SELECT * FROM {MIRROR} ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "agg",
            "filter(city).aggregate(Avg)",
            True,
            lambda: C.filter(city="city42").aggregate(a=Avg("score"))["a"],
            raw(WideCustomerU7, f"SELECT avg(score) AS id FROM {MIRROR} WHERE city = 'city42'"),
        ),
        (
            "agg",
            "filter(postcode).aggregate(Avg)",
            False,
            lambda: C.filter(postcode="pc42").aggregate(a=Avg("score"))["a"],
            raw(WideCustomerU7, f"SELECT avg(score) AS id FROM {MIRROR} WHERE postcode = 'pc42'"),
        ),
        (
            "join",
            "VIEW->VIEW  order.customer(city)",
            True,
            lambda: list(WideOrderU7.objects.filter(customer__city="city42")[:PAGE]),
            raw(
                WideOrderU7,
                f"SELECT o.* FROM {ORDER_MIRROR} o JOIN {MIRROR} c ON c.id = o.customer_id "
                f"WHERE c.city = 'city42' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "VIEW->VIEW  order.customer(postcode)",
            False,
            lambda: list(WideOrderU7.objects.filter(customer__postcode="pc42")[:PAGE]),
            raw(
                WideOrderU7,
                f"SELECT o.* FROM {ORDER_MIRROR} o JOIN {MIRROR} c ON c.id = o.customer_id "
                f"WHERE c.postcode = 'pc42' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "VIEW->PLAIN customer.region",
            None,
            lambda: list(WideCustomerU7.objects.filter(region__name="region7")[:PAGE]),
            raw(
                WideCustomerU7,
                f"SELECT c.* FROM {MIRROR} c JOIN testapp_wideregion r ON r.id = c.region_id "
                f"WHERE r.name = 'region7' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "prefetch instead of join",
            None,
            lambda: list(WideOrderU7.objects.prefetch_related("customer").order_by("id")[:PAGE]),
            raw(
                WideOrderU7,
                f"SELECT o.* FROM {ORDER_MIRROR} o JOIN {MIRROR} c ON c.id = o.customer_id ORDER BY o.id LIMIT {PAGE}",
            ),
        ),
    ]


def a_pk():
    return scalar(f"SELECT id FROM {MIRROR} LIMIT 1")


def measure(label_to_time):
    """One full pass, returning {label: overlay_ms or None}."""
    results = {}
    for _group, label, _indexed, overlay, _plain in shapes(a_pk()):
        overlay_ms, produced = best_of(overlay)
        if overlay_ms is not None:
            assert rows_of(produced), f"{label!r} measured an empty query"
        results[label] = overlay_ms
    label_to_time.update(results)
    return results


def fmt_ms(value):
    return f">{TIMEOUT_MS // 1000}s" if value is None else f"{value:.2f}ms"


def test_complexity():
    reference, doubled, thin = {}, {}, {}

    print(f"\n\n=== A: N={ROWS:,} rows, 50% materialised (the reference) ===")
    started = time.perf_counter()
    load(ROWS, 0.50)
    print(f"loaded in {time.perf_counter() - started:.0f}s")
    plain_times, produced_rows = {}, {}
    pk = a_pk()
    for _group, label, _indexed, overlay, plain in shapes(pk):
        overlay_ms, produced = best_of(overlay)
        plain_ms, _ = best_of(plain)
        if overlay_ms is not None:
            assert rows_of(produced), f"{label!r} measured an empty query"
        reference[label] = overlay_ms
        plain_times[label] = plain_ms
        produced_rows[label] = rows_of(produced) if produced is not None else 0

    print(f"\n=== B: N={ROWS * 2:,} rows, 50% materialised (for O(N)) ===")
    started = time.perf_counter()
    load(ROWS * 2, 0.50)
    print(f"loaded in {time.perf_counter() - started:.0f}s")
    measure(doubled)

    print(f"\n=== C: N={ROWS:,} rows, 5% materialised (for O(share)) ===")
    started = time.perf_counter()
    load(ROWS, 0.05)
    print(f"loaded in {time.perf_counter() - started:.0f}s")
    measure(thin)

    header = f"{'shape':<40}{'idx':>5}{'plain':>11}{'overlay':>11}{'ratio':>11}{'O(N)':>9}{'O(share)':>10}   rows"
    print(f"\n\n{'=' * len(header)}")
    print(
        f"N = {ROWS:,} view rows.  O(N): exponent k, time x2^k when rows double.  "
        f"O(share): x when 5% -> 50% materialised."
    )
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    group = None
    for case_group, label, indexed, _overlay, _plain in shapes(pk):
        if case_group != group:
            group = case_group
            print()
        a, b, c = reference[label], doubled[label], thin[label]
        plain = plain_times[label]

        ratio = "-" if (a is None or not plain) else f"x{a / plain:.2f}"
        if a and b:
            exponent = f"^{math.log2(b / a):+.2f}"
        else:
            exponent = "-"
        share = f"x{a / c:.2f}" if (a and c) else "-"
        marker = {True: "yes", False: "NO", None: "-"}[indexed]

        print(
            f"{label:<40}{marker:>5}{fmt_ms(plain):>11}{fmt_ms(a):>11}"
            f"{ratio:>11}{exponent:>9}{share:>10}   {produced_rows[label]}"
        )

    print("\nidx      = is the filter column indexed?")
    print("ratio    = overlay wall time / plain-table wall time")
    print("O(N)     = ^0.00 flat, ^1.00 linear in total rows, ^-x got faster (suspect noise)")
    print("O(share) = >1 means a more materialised view is slower; ~1 means the split doesn't matter")
