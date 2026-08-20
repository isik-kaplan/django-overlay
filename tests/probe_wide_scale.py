"""A realistically sized, realistically shaped benchmark.

Not run by CI, and not run by the default suite. It loads several million rows
and takes minutes. Run it deliberately:

    POSTGRES_USER=postgres uv run pytest tests/probe_wide_scale.py -s -q \
        -o addopts="" --no-cov

    OVERLAY_WIDE_SCALE=0.1 POSTGRES_USER=postgres uv run pytest ...   # a tenth

Everything the earlier probes measured was three narrow columns on one table
with no real overrides. This one has:

  * ~10 columns per table, mixed types, five indexed fields on WideCustomer and
    five deliberately unindexed ones, so indexed and unindexed access to the
    same rows can be compared directly;
  * **real overrides** -- 1,000,000 base rows carrying the id `-source.id`, so
    the view's anti-join actually excludes vendor rows, which no earlier probe
    exercised;
  * four overlay models and two plain ones, wired the way an application wires
    them: overlay -> plain (WideCustomer.region), plain -> overlay
    (WideCustomerNote.customer), and overlay -> overlay two hops deep
    (WideOrderLine -> WideOrder -> WideCustomer).

The comparison in every case is a plain table built with
`CREATE TABLE ... AS SELECT * FROM <the view>`, so its contents are identical to
what the view exposes, by construction, and it carries the same indexes.

Two traps carried over from the earlier probes: each case builds a fresh
QuerySet per round (evaluating one caches its rows), and each asserts it
produced rows before being timed (the negated id space makes empty results easy
to write by accident).
"""

import os
import time

import pytest
from django.db import OperationalError, connection
from django.db.models import Avg, Count

from tests.testapp.models import (
    WideCustomer,
    WideCustomerNote,
    WideOrder,
    WideOrderLine,
)


pytestmark = pytest.mark.django_db(transaction=True)

SCALE = float(os.environ.get("OVERLAY_WIDE_SCALE", 1.0))


def scaled(n):
    return max(1, int(n * SCALE))


# WideCustomer: 2,000,000 vendor rows; 2,000,000 base rows, of which 1,000,000
# are overrides of vendor rows and 1,000,000 are base-only. The view therefore
# exposes 2,000,000 + (2,000,000 - 1,000,000) = 3,000,000.
CUST_SOURCE = scaled(2_000_000)
CUST_OVERRIDE = scaled(1_000_000)
CUST_ORGANIC = scaled(1_000_000)

ORDER_SOURCE = scaled(1_000_000)
ORDER_OVERRIDE = scaled(200_000)
ORDER_ORGANIC = scaled(200_000)

PROD_SOURCE = scaled(100_000)
PROD_OVERRIDE = scaled(20_000)
PROD_ORGANIC = scaled(20_000)

LINE_SOURCE = scaled(1_000_000)
LINE_OVERRIDE = scaled(100_000)
LINE_ORGANIC = scaled(100_000)

REGIONS = 200
NOTES = scaled(300_000)

PAGE = 50
# A deep page, kept proportional so it still lands inside the data at any scale.
DEEP = scaled(500_000)
# Some shapes do not merely get slow at this size, they stop finishing. Cap
# them so one pathological query cannot stall the run, and report the cap.
TIMEOUT_MS = int(os.environ.get("OVERLAY_WIDE_TIMEOUT_MS", 30_000))

BASE_TABLES = ("widecustomer", "wideorder", "wideproduct", "wideorderline", "testapp_widecustomernote")
SOURCE_TABLES = (
    "testapp_shared_widecustomersource",
    "testapp_shared_wideordersource",
    "testapp_shared_wideproductsource",
    "testapp_shared_wideorderlinesource",
)
MIRRORS = ("bench_customer", "bench_order", "bench_product", "bench_line")


def sql(statement, *params):
    with connection.cursor() as cursor:
        cursor.execute(statement, params or None)


def scalar(statement):
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return cursor.fetchone()[0]


def best_of(fn, rounds=3, give_up_after_ms=1500):
    """Best of `rounds`, except for cases slow enough that repeating them would
    dominate the run -- one sample is precise enough at two seconds.

    Returns (milliseconds, produced, timed_out).
    """
    best, produced = None, None
    for _ in range(rounds):
        sql(f"SET statement_timeout = {TIMEOUT_MS}")
        started = time.perf_counter()
        try:
            produced = fn()
        except OperationalError:
            sql("SET statement_timeout = 0")
            return float(TIMEOUT_MS), None, True
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
        if best > give_up_after_ms:
            break
    sql("SET statement_timeout = 0")
    return best, produced, False


def reset():
    sql("SET CONSTRAINTS ALL IMMEDIATE")
    sql(f"TRUNCATE {', '.join(BASE_TABLES + SOURCE_TABLES + ('testapp_wideregion',))} CASCADE")
    sql("SET CONSTRAINTS ALL DEFERRED")
    for mirror in MIRRORS:
        sql(f"DROP TABLE IF EXISTS {mirror}")


def load():
    """Bulk-load through the base tables with their triggers off.

    The OverlayForeignKey constraint triggers fire once per row and would turn
    a few million inserts into an overnight job. They enforce referential
    integrity, which the generated data satisfies by construction -- every
    reference below points at an id the view really exposes.
    """
    reset()
    for table in BASE_TABLES:
        sql(f"ALTER TABLE {table} DISABLE TRIGGER USER")

    sql(
        "INSERT INTO testapp_wideregion (id, name, country) "
        "SELECT g, 'region' || g, CASE WHEN g %% 2 = 0 THEN 'GB' ELSE 'US' END FROM generate_series(1, %s) g",
        REGIONS,
    )

    # ------------------------------------------------------------- customers
    customer_columns = (
        "first_name, last_name, email, age, city, postcode, status, score, registered_on, notes, region_id"
    )
    customer_values = (
        "'first' || g, 'last' || (g %% 5000), 'e' || g || '@example.com', g %% 80, "
        "'city' || (g %% 1000), 'pc' || (g %% 9999), "
        "(ARRAY['active','lapsed','pending','closed'])[1 + g %% 4], g %% 1000, "
        "DATE '2015-01-01' + (g %% 3000), '', 1 + g %% " + str(REGIONS)
    )
    sql(
        f"INSERT INTO testapp_shared_widecustomersource (id, {customer_columns}) "
        f"SELECT g, {customer_values} FROM generate_series(1, %s) g",
        CUST_SOURCE,
    )
    # Overrides: id = -source.id, so the anti-join excludes the vendor row.
    sql(
        f"INSERT INTO widecustomer (id, {customer_columns}, _overlay_deleted) "
        f"SELECT -g, {customer_values}, FALSE FROM generate_series(1, %s) g",
        CUST_OVERRIDE,
    )
    # Base-only rows: positive ids, nothing in the vendor table to shadow.
    sql(
        f"INSERT INTO widecustomer (id, {customer_columns}, _overlay_deleted) "
        f"SELECT g, {customer_values}, FALSE FROM generate_series(1, %s) g",
        CUST_ORGANIC,
    )

    # -------------------------------------------------------------- products
    product_columns = "sku, name, category, price_cents, weight_grams, supplier, discontinued, description"
    product_values = (
        "'SKU' || g, 'product ' || g, 'cat' || (g %% 200), 100 + g %% 90000, g %% 5000, "
        "'supplier' || (g %% 300), g %% 17 = 0, ''"
    )
    sql(
        f"INSERT INTO testapp_shared_wideproductsource (id, {product_columns}) "
        f"SELECT g, {product_values} FROM generate_series(1, %s) g",
        PROD_SOURCE,
    )
    sql(
        f"INSERT INTO wideproduct (id, {product_columns}, _overlay_deleted) "
        f"SELECT -g, {product_values}, FALSE FROM generate_series(1, %s) g",
        PROD_OVERRIDE,
    )
    sql(
        f"INSERT INTO wideproduct (id, {product_columns}, _overlay_deleted) "
        f"SELECT g, {product_values}, FALSE FROM generate_series(1, %s) g",
        PROD_ORGANIC,
    )

    # ---------------------------------------------------------------- orders
    # customer_id alternates between a vendor-backed id (negative) and a
    # base-only id (positive), so joins traverse both halves of the union.
    customer_ref = f"CASE WHEN g %% 2 = 0 THEN -(1 + g %% {CUST_SOURCE}) ELSE (1 + g %% {CUST_ORGANIC}) END"
    order_columns = "reference, status, total_cents, placed_on, channel, currency, comment, customer_id"
    order_values = (
        "'REF' || g, (ARRAY['new','paid','shipped','refunded'])[1 + g %% 4], 500 + g %% 500000, "
        "DATE '2020-01-01' + (g %% 1500), (ARRAY['web','phone','store'])[1 + g %% 3], 'GBP', '', "
        f"{customer_ref}"
    )
    sql(
        f"INSERT INTO testapp_shared_wideordersource (id, {order_columns}) "
        f"SELECT g, {order_values} FROM generate_series(1, %s) g",
        ORDER_SOURCE,
    )
    sql(
        f"INSERT INTO wideorder (id, {order_columns}, _overlay_deleted) "
        f"SELECT -g, {order_values}, FALSE FROM generate_series(1, %s) g",
        ORDER_OVERRIDE,
    )
    sql(
        f"INSERT INTO wideorder (id, {order_columns}, _overlay_deleted) "
        f"SELECT g, {order_values}, FALSE FROM generate_series(1, %s) g",
        ORDER_ORGANIC,
    )

    # ----------------------------------------------------------- order lines
    order_ref = f"CASE WHEN g %% 2 = 0 THEN -(1 + g %% {ORDER_SOURCE}) ELSE (1 + g %% {ORDER_ORGANIC}) END"
    product_ref = f"CASE WHEN g %% 3 = 0 THEN -(1 + g %% {PROD_SOURCE}) ELSE (1 + g %% {PROD_ORGANIC}) END"
    line_columns = "quantity, unit_price_cents, discount_cents, note, order_id, product_id"
    line_values = f"1 + g %% 9, 100 + g %% 50000, g %% 500, '', {order_ref}, {product_ref}"
    sql(
        f"INSERT INTO testapp_shared_wideorderlinesource (id, {line_columns}) "
        f"SELECT g, {line_values} FROM generate_series(1, %s) g",
        LINE_SOURCE,
    )
    sql(
        f"INSERT INTO wideorderline (id, {line_columns}, _overlay_deleted) "
        f"SELECT -g, {line_values}, FALSE FROM generate_series(1, %s) g",
        LINE_OVERRIDE,
    )
    sql(
        f"INSERT INTO wideorderline (id, {line_columns}, _overlay_deleted) "
        f"SELECT g, {line_values}, FALSE FROM generate_series(1, %s) g",
        LINE_ORGANIC,
    )

    # ----------------------------------------------------------------- notes
    sql(
        "INSERT INTO testapp_widecustomernote (id, body, author, customer_id) "
        f"SELECT g, 'note ' || g, 'author' || (g %% 50), {customer_ref} FROM generate_series(1, %s) g",
        NOTES,
    )

    for table in BASE_TABLES:
        sql(f"ALTER TABLE {table} ENABLE TRIGGER USER")

    # ------------------------------------------- indexes on the vendor tables
    # The expression index on the negated pk is the one that matters most; see
    # docs/operations/PERFORMANCE.md.
    for table in SOURCE_TABLES:
        sql(f"CREATE INDEX IF NOT EXISTS wi_{table}_negid ON {table} ((-id))")
    for column in ("last_name", "city", "status", "score", "age"):
        sql(f"CREATE INDEX IF NOT EXISTS wi_cust_{column} ON testapp_shared_widecustomersource ({column})")
    for column in ("status", "channel", "customer_id"):
        sql(f"CREATE INDEX IF NOT EXISTS wi_ord_{column} ON testapp_shared_wideordersource ({column})")
    for column in ("category", "sku"):
        sql(f"CREATE INDEX IF NOT EXISTS wi_prod_{column} ON testapp_shared_wideproductsource ({column})")
    for column in ("order_id", "product_id"):
        sql(f"CREATE INDEX IF NOT EXISTS wi_line_{column} ON testapp_shared_wideorderlinesource ({column})")

    # --------------------------------------------------- the plain-table copy
    # Built from the views, so the contents are identical by construction.
    for mirror, view in (
        ("bench_customer", "widecustomer_view"),
        ("bench_order", "wideorder_view"),
        ("bench_product", "wideproduct_view"),
        ("bench_line", "wideorderline_view"),
    ):
        sql(f"CREATE TABLE {mirror} AS SELECT * FROM {view}")
        sql(f"ALTER TABLE {mirror} ADD PRIMARY KEY (id)")
    for column in ("last_name", "city", "status", "score", "age"):
        sql(f"CREATE INDEX bench_customer_{column} ON bench_customer ({column})")
    for column in ("status", "channel", "customer_id"):
        sql(f"CREATE INDEX bench_order_{column} ON bench_order ({column})")
    for column in ("category", "sku"):
        sql(f"CREATE INDEX bench_product_{column} ON bench_product ({column})")
    for column in ("order_id", "product_id"):
        sql(f"CREATE INDEX bench_line_{column} ON bench_line ({column})")

    for table in BASE_TABLES + SOURCE_TABLES + MIRRORS + ("testapp_wideregion",):
        sql(f"ANALYZE {table}")


def raw(model, statement, *params):
    return lambda: list(model.objects.raw(statement, list(params)))


def n(result):
    if isinstance(result, int):
        return result
    try:
        return len(result)
    except TypeError:
        return 1


# A vendor-backed customer id (negative), an override id, and a base-only id.
VENDOR_PK = -(CUST_OVERRIDE + CUST_SOURCE // 4)
OVERRIDE_PK = -(CUST_OVERRIDE // 2)
ORGANIC_PK = CUST_ORGANIC // 2


def cases():
    """(group, label, overlay, plain)."""
    C, P = WideCustomer.objects, "bench_customer"

    return [
        # ------------------------------------------------------ point lookups
        (
            "lookup",
            "get(pk=…) vendor-backed row",
            lambda: [C.get(pk=VENDOR_PK)],
            raw(WideCustomer, f"SELECT * FROM {P} WHERE id = %s", VENDOR_PK),
        ),
        (
            "lookup",
            "get(pk=…) overridden row",
            lambda: [C.get(pk=OVERRIDE_PK)],
            raw(WideCustomer, f"SELECT * FROM {P} WHERE id = %s", OVERRIDE_PK),
        ),
        (
            "lookup",
            "get(pk=…) base-only row",
            lambda: [C.get(pk=ORGANIC_PK)],
            raw(WideCustomer, f"SELECT * FROM {P} WHERE id = %s", ORGANIC_PK),
        ),
        # ------------------------------------------------- indexed vs not
        (
            "filter",
            "filter(city=…)            INDEXED",
            lambda: list(C.filter(city="city42")[:PAGE]),
            raw(WideCustomer, f"SELECT * FROM {P} WHERE city = 'city42' LIMIT {PAGE}"),
        ),
        (
            "filter",
            "filter(postcode=…)        unindexed",
            lambda: list(C.filter(postcode="pc42")[:PAGE]),
            raw(WideCustomer, f"SELECT * FROM {P} WHERE postcode = 'pc42' LIMIT {PAGE}"),
        ),
        (
            "filter",
            "filter(city=…).count()    INDEXED",
            lambda: C.filter(city="city42").count(),
            raw(WideCustomer, f"SELECT count(*) AS id FROM {P} WHERE city = 'city42'"),
        ),
        (
            "filter",
            "filter(postcode=…).count() unindexed",
            lambda: C.filter(postcode="pc42").count(),
            raw(WideCustomer, f"SELECT count(*) AS id FROM {P} WHERE postcode = 'pc42'"),
        ),
        (
            "filter",
            "count() everything",
            lambda: C.count(),
            raw(WideCustomer, f"SELECT count(*) AS id FROM {P}"),
        ),
        # ------------------------------------------------------------ ordering
        (
            "order",
            "filter(city).order_by('id')[:50]      INDEXED",
            lambda: list(C.filter(city="city42").order_by("id")[:PAGE]),
            raw(WideCustomer, f"SELECT * FROM {P} WHERE city = 'city42' ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "order",
            "filter(city).order_by('score')[:50]   INDEXED sort",
            lambda: list(C.filter(city="city42").order_by("score")[:PAGE]),
            raw(WideCustomer, f"SELECT * FROM {P} WHERE city = 'city42' ORDER BY score LIMIT {PAGE}"),
        ),
        (
            "order",
            "filter(city).order_by('email')[:50]   unindexed sort",
            lambda: list(C.filter(city="city42").order_by("email")[:PAGE]),
            raw(WideCustomer, f"SELECT * FROM {P} WHERE city = 'city42' ORDER BY email LIMIT {PAGE}"),
        ),
        (
            "order",
            "order_by('id')[:50]                   no filter",
            lambda: list(C.order_by("id")[:PAGE]),
            raw(WideCustomer, f"SELECT * FROM {P} ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "order",
            "order_by('score')[:50]                no filter, INDEXED",
            lambda: list(C.order_by("score")[:PAGE]),
            raw(WideCustomer, f"SELECT * FROM {P} ORDER BY score LIMIT {PAGE}"),
        ),
        (
            "order",
            "order_by('email')[:50]                no filter, unindexed",
            lambda: list(C.order_by("email")[:PAGE]),
            raw(WideCustomer, f"SELECT * FROM {P} ORDER BY email LIMIT {PAGE}"),
        ),
        (
            "order",
            "order_by('id')[deep:deep+50]           deep offset",
            lambda: list(C.order_by("id")[DEEP : DEEP + PAGE]),
            raw(WideCustomer, f"SELECT * FROM {P} ORDER BY id LIMIT {PAGE} OFFSET {DEEP}"),
        ),
        # ------------------------------------------------- distinct, aggregates
        (
            "agg",
            "filter(city).distinct()[:50]          INDEXED",
            lambda: list(C.filter(city="city42").distinct().order_by("id")[:PAGE]),
            raw(WideCustomer, f"SELECT DISTINCT * FROM {P} WHERE city = 'city42' ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "agg",
            "distinct().order_by('id')[:50]        no filter",
            lambda: list(C.distinct().order_by("id")[:PAGE]),
            raw(WideCustomer, f"SELECT DISTINCT * FROM {P} ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "agg",
            "values('status').annotate(Count)      INDEXED",
            lambda: list(C.values("status").annotate(c=Count("id"))),
            raw(WideCustomer, f"SELECT status AS id, count(*) AS c FROM {P} GROUP BY status"),
        ),
        (
            "agg",
            "values('postcode').annotate(Count)    unindexed",
            lambda: list(C.values("postcode").annotate(c=Count("id"))),
            raw(WideCustomer, f"SELECT postcode AS id, count(*) AS c FROM {P} GROUP BY postcode"),
        ),
        (
            "agg",
            "filter(city).aggregate(Avg('score'))  INDEXED",
            lambda: C.filter(city="city42").aggregate(a=Avg("score")),
            raw(WideCustomer, f"SELECT 1 AS id, avg(score) AS a FROM {P} WHERE city = 'city42'"),
        ),
        (
            "agg",
            "aggregate(Avg('score'))               no filter",
            lambda: C.aggregate(a=Avg("score")),
            raw(WideCustomer, f"SELECT 1 AS id, avg(score) AS a FROM {P}"),
        ),
        ("agg", "exists()", lambda: [C.exists()], raw(WideCustomer, f"SELECT * FROM {P} LIMIT 1")),
        # ------------------------------------------------------- joins, 2 views
        (
            "join",
            "Order.filter(customer__city=…)        overlay->overlay",
            lambda: list(WideOrder.objects.filter(customer__city="city42")[:PAGE]),
            raw(
                WideOrder,
                "SELECT o.* FROM bench_order o JOIN bench_customer c ON c.id = o.customer_id "
                f"WHERE c.city = 'city42' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "Order.filter(customer__postcode=…)    overlay->overlay, unindexed",
            lambda: list(WideOrder.objects.filter(customer__postcode="pc42")[:PAGE]),
            raw(
                WideOrder,
                "SELECT o.* FROM bench_order o JOIN bench_customer c ON c.id = o.customer_id "
                f"WHERE c.postcode = 'pc42' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "Customer.filter(region__name=…)       overlay->plain",
            lambda: list(WideCustomer.objects.filter(region__name="region7")[:PAGE]),
            raw(
                WideCustomer,
                "SELECT c.* FROM bench_customer c JOIN testapp_wideregion r ON r.id = c.region_id "
                f"WHERE r.name = 'region7' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "Note.filter(customer__city=…)         plain->overlay",
            lambda: list(WideCustomerNote.objects.filter(customer__city="city42")[:PAGE]),
            raw(
                WideCustomer,
                "SELECT n.id, n.body, n.author, n.customer_id FROM testapp_widecustomernote n "
                f"JOIN bench_customer c ON c.id = n.customer_id WHERE c.city = 'city42' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "Customer.annotate(Count('orders'))    reverse overlay FK",
            lambda: list(WideCustomer.objects.filter(city="city42").annotate(c=Count("orders")).order_by("id")[:PAGE]),
            raw(
                WideCustomer,
                "SELECT c.*, count(o.id) AS c2 FROM bench_customer c "
                "LEFT JOIN bench_order o ON o.customer_id = c.id "
                f"WHERE c.city = 'city42' GROUP BY c.id ORDER BY c.id LIMIT {PAGE}",
            ),
        ),
        # ------------------------------------------------------ joins, 3+ views
        (
            "multijoin",
            "Line.filter(order__customer__city=…)  3 views deep",
            lambda: list(WideOrderLine.objects.filter(order__customer__city="city42")[:PAGE]),
            raw(
                WideOrderLine,
                "SELECT l.* FROM bench_line l JOIN bench_order o ON o.id = l.order_id "
                f"JOIN bench_customer c ON c.id = o.customer_id WHERE c.city = 'city42' LIMIT {PAGE}",
            ),
        ),
        (
            "multijoin",
            "Line.filter(product__category=…)      overlay->overlay",
            lambda: list(WideOrderLine.objects.filter(product__category="cat7")[:PAGE]),
            raw(
                WideOrderLine,
                "SELECT l.* FROM bench_line l JOIN bench_product p ON p.id = l.product_id "
                f"WHERE p.category = 'cat7' LIMIT {PAGE}",
            ),
        ),
        (
            "multijoin",
            "Line ... order__customer__region__name 3 views + plain",
            lambda: list(WideOrderLine.objects.filter(order__customer__region__name="region7")[:PAGE]),
            raw(
                WideOrderLine,
                "SELECT l.* FROM bench_line l JOIN bench_order o ON o.id = l.order_id "
                "JOIN bench_customer c ON c.id = o.customer_id "
                f"JOIN testapp_wideregion r ON r.id = c.region_id WHERE r.name = 'region7' LIMIT {PAGE}",
            ),
        ),
        (
            "multijoin",
            "Line ... two branches (product + customer)",
            lambda: list(
                WideOrderLine.objects.filter(product__category="cat7", order__customer__status="active")[:PAGE]
            ),
            raw(
                WideOrderLine,
                "SELECT l.* FROM bench_line l JOIN bench_order o ON o.id = l.order_id "
                "JOIN bench_customer c ON c.id = o.customer_id "
                "JOIN bench_product p ON p.id = l.product_id "
                f"WHERE p.category = 'cat7' AND c.status = 'active' LIMIT {PAGE}",
            ),
        ),
        (
            "multijoin",
            "select_related('order__customer')     3 views, one query",
            lambda: list(WideOrderLine.objects.select_related("order__customer").order_by("id")[:PAGE]),
            raw(
                WideOrderLine,
                "SELECT l.* FROM bench_line l JOIN bench_order o ON o.id = l.order_id "
                f"JOIN bench_customer c ON c.id = o.customer_id ORDER BY l.id LIMIT {PAGE}",
            ),
        ),
    ]


def test_wide_scale():
    started = time.perf_counter()
    load()
    load_seconds = time.perf_counter() - started

    print(f"\n\n=== loaded in {load_seconds:.0f}s ===")
    print(f"{'table':<38} {'rows':>12}  {'size':>10}")
    for label, table in (
        ("widecustomer (base: 1M override + 1M own)", "widecustomer"),
        ("  widecustomersource (vendor)", "testapp_shared_widecustomersource"),
        ("  widecustomer_view (what Django sees)", "widecustomer_view"),
        ("  bench_customer (the comparison)", "bench_customer"),
        ("wideorder_view", "wideorder_view"),
        ("wideproduct_view", "wideproduct_view"),
        ("wideorderline_view", "wideorderline_view"),
        ("testapp_widecustomernote (plain)", "testapp_widecustomernote"),
    ):
        rows = scalar(f"SELECT count(*) FROM {table}")
        size = "-"
        if not table.endswith("_view"):
            size = scalar(f"SELECT pg_size_pretty(pg_total_relation_size('{table}'))")
        print(f"{label:<38} {rows:>12,}  {size:>10}")

    shadowed = scalar(
        "SELECT count(*) FROM testapp_shared_widecustomersource s "
        "WHERE EXISTS (SELECT 1 FROM widecustomer b WHERE b.id = -s.id)"
    )
    print(f"\nvendor rows actually shadowed by an override: {shadowed:,}")

    measured = []
    for group, label, overlay, plain in cases():
        overlay_ms, produced, overlay_out = best_of(overlay)
        if not overlay_out:
            assert n(produced), f"{label} produced nothing"
        plain_ms, _, plain_out = best_of(plain)
        measured.append((group, label, plain_ms, plain_out, overlay_ms, overlay_out, 0 if overlay_out else n(produced)))

    width = max(len(label) for _, label, *_ in measured)
    print("\n=== overlay vs one plain table holding the identical rows ===")
    print(f"{'group':<10} {'query':<{width}}  {'plain':>11}  {'overlay':>11}  {'ratio':>8}  {'rows':>7}")
    for group, label, plain_ms, plain_out, overlay_ms, overlay_out, rows in measured:
        plain_cell = f">{plain_ms / 1000:.0f}s" if plain_out else f"{plain_ms:.1f}ms"
        overlay_cell = f">{overlay_ms / 1000:.0f}s" if overlay_out else f"{overlay_ms:.1f}ms"
        ratio = "TIMEOUT" if overlay_out else f"{overlay_ms / plain_ms:.1f}x"
        print(f"{group:<10} {label:<{width}}  {plain_cell:>11}  {overlay_cell:>11}  {ratio:>8}  {rows:>7,}")

    reset()
