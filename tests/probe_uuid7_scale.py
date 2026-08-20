"""The wide benchmark, under a uuid strategy.

`probe_wide_scale.py` measures the same schema under `NEGATIVE_ID`. Every
headline number in the project came from it, and UUID7 is the strategy that
will actually ship, so the two need to be readable side by side. The shape
labels here match that file's deliberately.

Why the two probes are separate rather than one parameterised file: the loader
is the part that genuinely differs. `NEGATIVE_ID` can generate ids as `g` and
`-g` straight out of `generate_series`; a uuid strategy needs a deterministic
uuid per series index so that foreign keys line up, which changes every INSERT.

    POSTGRES_USER=postgres uv run pytest tests/probe_uuid7_scale.py -s -q \
        -o addopts="" --no-cov

    OVERLAY_WIDE_SCALE=0.05 POSTGRES_USER=postgres uv run pytest ...   # quick

What to expect, from the 1,500,000-row strategy comparison run earlier: the
non-join shapes were within noise between strategies, and the view-to-view join
was 17x better under uuid, because `base.id = source.id` is a plain column
equality the planner can drive from both primary key indexes -- where
`base.id = -source.id` is an expression with only expression-index statistics.
"""

import os
import time

import pytest
from django.db import OperationalError, connection
from django.db.models import Avg, Count

from tests.testapp.models import (
    WideCustomerNoteU7,
    WideCustomerU7,
    WideOrderLineU7,
    WideOrderU7,
)


pytestmark = pytest.mark.django_db(transaction=True)

SCALE = float(os.environ.get("OVERLAY_WIDE_SCALE", 1.0))


def scaled(n):
    return max(1, int(n * SCALE))


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
DEEP = scaled(500_000)
TIMEOUT_MS = int(os.environ.get("OVERLAY_WIDE_TIMEOUT_MS", 30_000))

# The vendor tables ship with nothing but a primary key, while the base
# tables carry five indexes each and so does the plain comparison table.
# The view reads both branches, so an unindexed source turns every filter
# that cannot terminate early into a sequential scan of the whole vendor
# table -- and the "overlay is slow" numbers then include a cost no
# correctly configured deployment would pay. `show_source_indexes` exists
# to catch exactly this. Set OVERLAY_INDEX_SOURCES=1 to mirror the base
# indexes onto the source and measure the difference.
INDEX_SOURCES = os.environ.get("OVERLAY_INDEX_SOURCES") == "1"

SOURCE_INDEXES = {
    "testapp_shared_widecustomeru7source": ("last_name", "city", "status", "score", "age"),
    "testapp_shared_wideorderu7source": ("status", "channel", "customer_id"),
    "testapp_shared_wideproductu7source": ("category", "sku"),
    "testapp_shared_wideorderlineu7source": ("order_id", "product_id"),
}

# Organic rows live in a disjoint id space so they can never collide with a
# source row. Under NEGATIVE_ID the sign does that job; here it is an offset.
ORGANIC_OFFSET = 100_000_000

# A deterministic uuid per series index, monotonic in g so it mimics uuid7's
# time ordering rather than uuid4's scatter -- index locality is part of what
# is being measured. 12 hex of counter + 1 version nibble + 3 + 1 variant + 15.
UUID_EXPR = (
    "(lpad(to_hex({g}), 12, '0') || '7' || substr(md5({g}::text), 1, 3)"
    " || '8' || substr(md5({g}::text || 'x'), 1, 15))::uuid"
)


def u(g) -> str:
    return UUID_EXPR.format(g=g)


BASE_TABLES = (
    "widecustomer_u7",
    "wideorder_u7",
    "wideproduct_u7",
    "wideorderline_u7",
    "testapp_widecustomernoteu7",
)
SOURCE_TABLES = (
    "testapp_shared_widecustomeru7source",
    "testapp_shared_wideorderu7source",
    "testapp_shared_wideproductu7source",
    "testapp_shared_wideorderlineu7source",
)
MIRRORS = ("bu7_customer", "bu7_order", "bu7_product", "bu7_line")


def sql(statement, *params):
    with connection.cursor() as cursor:
        cursor.execute(statement, params or None)


def scalar(statement):
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return cursor.fetchone()[0]


def best_of(fn, rounds=3, give_up_after_ms=1500):
    """Best of `rounds`; returns (milliseconds, produced, timed_out)."""
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
    sql(f"TRUNCATE {', '.join(BASE_TABLES + SOURCE_TABLES)} CASCADE")
    sql("SET CONSTRAINTS ALL DEFERRED")
    for mirror in MIRRORS:
        sql(f"DROP TABLE IF EXISTS {mirror}")


def load():
    """Same row counts and same column values as probe_wide_scale, so the two
    runs differ only in how ids are minted.

    Triggers off during the load: the OverlayForeignKey constraint triggers
    fire once per row and would turn a few million inserts into an overnight
    job. Every reference generated below points at an id the view really
    exposes, which is what those triggers would have checked."""
    reset()
    for table in BASE_TABLES:
        sql(f"ALTER TABLE {table} DISABLE TRIGGER USER")

    if scalar("SELECT count(*) FROM testapp_wideregion") == 0:
        sql(
            "INSERT INTO testapp_wideregion (id, name, country) "
            "SELECT g, 'region' || g, CASE WHEN g %% 2 = 0 THEN 'GB' ELSE 'US' END "
            "FROM generate_series(1, %s) g",
            REGIONS,
        )

    # ------------------------------------------------------------- customers
    cust_cols = "first_name, last_name, email, age, city, postcode, status, score, registered_on, notes, region_id"
    cust_vals = (
        "'first' || g, 'last' || (g %% 5000), 'e' || g || '@example.com', g %% 80, "
        "'city' || (g %% 1000), 'pc' || (g %% 9999), "
        "(ARRAY['active','lapsed','pending','closed'])[1 + g %% 4], g %% 1000, "
        "DATE '2015-01-01' + (g %% 3000), '', 1 + g %% " + str(REGIONS)
    )
    sql(
        f"INSERT INTO testapp_shared_widecustomeru7source (id, {cust_cols}) "
        f"SELECT {u('g')}, {cust_vals} FROM generate_series(1, %s) g",
        CUST_SOURCE,
    )
    # Overrides carry the *same* uuid as their source row, so the anti-join
    # excludes the vendor row. No negation anywhere -- that is the point.
    sql(
        f"INSERT INTO widecustomer_u7 (id, {cust_cols}, _overlay_deleted) "
        f"SELECT {u('g')}, {cust_vals}, FALSE FROM generate_series(1, %s) g",
        CUST_OVERRIDE,
    )
    sql(
        f"INSERT INTO widecustomer_u7 (id, {cust_cols}, _overlay_deleted) "
        f"SELECT {u(f'(g + {ORGANIC_OFFSET})')}, {cust_vals}, FALSE FROM generate_series(1, %s) g",
        CUST_ORGANIC,
    )

    # -------------------------------------------------------------- products
    prod_cols = "sku, name, category, price_cents, weight_grams, supplier, discontinued, description"
    prod_vals = (
        "'SKU' || g, 'product ' || g, 'cat' || (g %% 200), 100 + g %% 90000, g %% 5000, "
        "'supplier' || (g %% 300), g %% 17 = 0, ''"
    )
    sql(
        f"INSERT INTO testapp_shared_wideproductu7source (id, {prod_cols}) "
        f"SELECT {u('g')}, {prod_vals} FROM generate_series(1, %s) g",
        PROD_SOURCE,
    )
    sql(
        f"INSERT INTO wideproduct_u7 (id, {prod_cols}, _overlay_deleted) "
        f"SELECT {u('g')}, {prod_vals}, FALSE FROM generate_series(1, %s) g",
        PROD_OVERRIDE,
    )
    sql(
        f"INSERT INTO wideproduct_u7 (id, {prod_cols}, _overlay_deleted) "
        f"SELECT {u(f'(g + {ORGANIC_OFFSET})')}, {prod_vals}, FALSE FROM generate_series(1, %s) g",
        PROD_ORGANIC,
    )

    # ---------------------------------------------------------------- orders
    # Half the orders point at a vendor-backed customer, half at an organic
    # one, so the join has to cross both branches of the customer view.
    cust_ref = (
        f"CASE WHEN g %% 2 = 0 THEN {u(f'(1 + g %% {CUST_SOURCE})')} "
        f"ELSE {u(f'({ORGANIC_OFFSET} + 1 + g %% {CUST_ORGANIC})')} END"
    )
    order_cols = "reference, status, total_cents, placed_on, channel, currency, comment, customer_id"
    order_vals = (
        "'REF' || g, (ARRAY['new','paid','shipped','cancelled'])[1 + g %% 4], "
        "100 + g %% 500000, DATE '2020-01-01' + (g %% 1500), "
        "(ARRAY['web','phone','store'])[1 + g %% 3], 'GBP', '', " + cust_ref
    )
    sql(
        f"INSERT INTO testapp_shared_wideorderu7source (id, {order_cols}) "
        f"SELECT {u('g')}, {order_vals} FROM generate_series(1, %s) g",
        ORDER_SOURCE,
    )
    sql(
        f"INSERT INTO wideorder_u7 (id, {order_cols}, _overlay_deleted) "
        f"SELECT {u('g')}, {order_vals}, FALSE FROM generate_series(1, %s) g",
        ORDER_OVERRIDE,
    )
    sql(
        f"INSERT INTO wideorder_u7 (id, {order_cols}, _overlay_deleted) "
        f"SELECT {u(f'(g + {ORGANIC_OFFSET})')}, {order_vals}, FALSE FROM generate_series(1, %s) g",
        ORDER_ORGANIC,
    )

    # ----------------------------------------------------------- order lines
    order_ref = (
        f"CASE WHEN g %% 2 = 0 THEN {u(f'(1 + g %% {ORDER_SOURCE})')} "
        f"ELSE {u(f'({ORGANIC_OFFSET} + 1 + g %% {ORDER_ORGANIC})')} END"
    )
    prod_ref = (
        f"CASE WHEN g %% 3 = 0 THEN {u(f'(1 + g %% {PROD_SOURCE})')} "
        f"ELSE {u(f'({ORGANIC_OFFSET} + 1 + g %% {PROD_ORGANIC})')} END"
    )
    line_cols = "quantity, unit_price_cents, discount_cents, note, order_id, product_id"
    line_vals = f"1 + g %% 9, 100 + g %% 40000, g %% 500, '', {order_ref}, {prod_ref}"
    sql(
        f"INSERT INTO testapp_shared_wideorderlineu7source (id, {line_cols}) "
        f"SELECT {u('g')}, {line_vals} FROM generate_series(1, %s) g",
        LINE_SOURCE,
    )
    sql(
        f"INSERT INTO wideorderline_u7 (id, {line_cols}, _overlay_deleted) "
        f"SELECT {u('g')}, {line_vals}, FALSE FROM generate_series(1, %s) g",
        LINE_OVERRIDE,
    )
    sql(
        f"INSERT INTO wideorderline_u7 (id, {line_cols}, _overlay_deleted) "
        f"SELECT {u(f'(g + {ORGANIC_OFFSET})')}, {line_vals}, FALSE FROM generate_series(1, %s) g",
        LINE_ORGANIC,
    )

    # ------------------------------------------- a plain table -> the view
    sql(
        f"INSERT INTO testapp_widecustomernoteu7 (customer_id, body, author) "
        f"SELECT {cust_ref}, 'note ' || g, 'author' || (g %% 50) FROM generate_series(1, %s) g",
        NOTES,
    )

    for table in BASE_TABLES:
        sql(f"ALTER TABLE {table} ENABLE TRIGGER USER")

    # ---------------------------------------- plain mirrors of each view
    # Built from the view itself, so their contents are identical to what the
    # view exposes by construction, and they carry the same indexes.
    for mirror, view in (
        ("bu7_customer", "widecustomer_u7_view"),
        ("bu7_order", "wideorder_u7_view"),
        ("bu7_product", "wideproduct_u7_view"),
        ("bu7_line", "wideorderline_u7_view"),
    ):
        sql(f"CREATE TABLE {mirror} AS SELECT * FROM {view}")
        sql(f"ALTER TABLE {mirror} ADD PRIMARY KEY (id)")

    for column in ("last_name", "city", "status", "score", "age"):
        sql(f"CREATE INDEX bu7_customer_{column} ON bu7_customer ({column})")
    for column in ("status", "channel", "customer_id"):
        sql(f"CREATE INDEX bu7_order_{column} ON bu7_order ({column})")
    for column in ("category", "sku"):
        sql(f"CREATE INDEX bu7_product_{column} ON bu7_product ({column})")
    for column in ("order_id", "product_id"):
        sql(f"CREATE INDEX bu7_line_{column} ON bu7_line ({column})")

    if INDEX_SOURCES:
        for table, columns in SOURCE_INDEXES.items():
            for column in columns:
                sql(f"CREATE INDEX IF NOT EXISTS src_idx_{table}_{column} ON {table} ({column})")

    for table in BASE_TABLES + SOURCE_TABLES + MIRRORS + ("testapp_wideregion",):
        sql(f"ANALYZE {table}")


def raw(model, statement, *params):
    def run():
        return list(model.objects.raw(statement, params))

    return run


def n(result):
    """Row count for the display column, and the non-empty guard.

    Returns an int on purpose, so `assert n(...)` actually fires on an empty
    result — timing an empty query is the most repeated mistake in this
    project's benchmarking. Same shape as probe_wide_scale's."""
    if isinstance(result, int):
        return result
    try:
        return len(result)
    except TypeError:
        return 1


def pks():
    """One vendor-backed, one overridden and one organic customer, read out of
    the data rather than computed, so a loader change can't silently make these
    miss."""
    vendor = scalar(
        "SELECT id FROM testapp_shared_widecustomeru7source s "
        "WHERE NOT EXISTS (SELECT 1 FROM widecustomer_u7 b WHERE b.id = s.id) LIMIT 1"
    )
    override = scalar(
        "SELECT id FROM widecustomer_u7 b "
        "WHERE EXISTS (SELECT 1 FROM testapp_shared_widecustomeru7source s WHERE s.id = b.id) LIMIT 1"
    )
    organic = scalar(
        "SELECT id FROM widecustomer_u7 b "
        "WHERE NOT EXISTS (SELECT 1 FROM testapp_shared_widecustomeru7source s WHERE s.id = b.id) LIMIT 1"
    )
    return vendor, override, organic


def _three_queries():
    """The 3-view join decomposed into three pk-indexed lookups.

    Not exactly equivalent -- the LIMIT lands in a different place -- but it is
    what an application would actually write if the join could not be made to
    finish, so it is worth knowing what it costs."""
    customer_ids = list(WideCustomerU7.objects.filter(city="city42").values_list("id", flat=True))
    order_ids = list(WideOrderU7.objects.filter(customer_id__in=customer_ids).values_list("id", flat=True))
    return list(WideOrderLineU7.objects.filter(order_id__in=order_ids)[:PAGE])


def cases(vendor_pk, override_pk, organic_pk):
    """(group, label, overlay, plain) — labels match probe_wide_scale."""
    C, P = WideCustomerU7.objects, "bu7_customer"

    return [
        (
            "lookup",
            "get(pk=…) vendor-backed row",
            lambda: [C.get(pk=vendor_pk)],
            raw(WideCustomerU7, f"SELECT * FROM {P} WHERE id = %s", vendor_pk),
        ),
        (
            "lookup",
            "get(pk=…) overridden row",
            lambda: [C.get(pk=override_pk)],
            raw(WideCustomerU7, f"SELECT * FROM {P} WHERE id = %s", override_pk),
        ),
        (
            "lookup",
            "get(pk=…) base-only row",
            lambda: [C.get(pk=organic_pk)],
            raw(WideCustomerU7, f"SELECT * FROM {P} WHERE id = %s", organic_pk),
        ),
        (
            "filter",
            "filter(city=…)            INDEXED",
            lambda: list(C.filter(city="city42")[:PAGE]),
            raw(WideCustomerU7, f"SELECT * FROM {P} WHERE city = 'city42' LIMIT {PAGE}"),
        ),
        (
            "filter",
            "filter(postcode=…)        unindexed",
            lambda: list(C.filter(postcode="pc42")[:PAGE]),
            raw(WideCustomerU7, f"SELECT * FROM {P} WHERE postcode = 'pc42' LIMIT {PAGE}"),
        ),
        (
            "filter",
            "filter(city=…).count()    INDEXED",
            lambda: C.filter(city="city42").count(),
            raw(WideCustomerU7, f"SELECT count(*) AS id FROM {P} WHERE city = 'city42'"),
        ),
        ("filter", "count() everything", lambda: C.count(), raw(WideCustomerU7, f"SELECT count(*) AS id FROM {P}")),
        (
            "order",
            "filter(city).order_by('id')[:50]      INDEXED",
            lambda: list(C.filter(city="city42").order_by("id")[:PAGE]),
            raw(WideCustomerU7, f"SELECT * FROM {P} WHERE city = 'city42' ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "order",
            "filter(city).order_by('score')[:50]   INDEXED sort",
            lambda: list(C.filter(city="city42").order_by("score")[:PAGE]),
            raw(WideCustomerU7, f"SELECT * FROM {P} WHERE city = 'city42' ORDER BY score LIMIT {PAGE}"),
        ),
        (
            "order",
            "filter(city).order_by('email')[:50]   unindexed sort",
            lambda: list(C.filter(city="city42").order_by("email")[:PAGE]),
            raw(WideCustomerU7, f"SELECT * FROM {P} WHERE city = 'city42' ORDER BY email LIMIT {PAGE}"),
        ),
        (
            "order",
            "order_by('id')[:50]                   no filter",
            lambda: list(C.order_by("id")[:PAGE]),
            raw(WideCustomerU7, f"SELECT * FROM {P} ORDER BY id LIMIT {PAGE}"),
        ),
        (
            "order",
            "order_by('score')[:50]                no filter",
            lambda: list(C.order_by("score")[:PAGE]),
            raw(WideCustomerU7, f"SELECT * FROM {P} ORDER BY score LIMIT {PAGE}"),
        ),
        (
            "order",
            "last()",
            lambda: [C.order_by("id").last()],
            raw(WideCustomerU7, f"SELECT * FROM {P} ORDER BY id DESC LIMIT 1"),
        ),
        # Unfiltered on purpose. `city42` matches one row in a thousand, so a
        # filtered offset of DEEP lands past the end of the result at every
        # scale and would time an empty query.
        (
            "page",
            "order_by('id')[deep:deep+50]           deep offset",
            lambda: list(C.order_by("id")[DEEP : DEEP + PAGE]),
            raw(WideCustomerU7, f"SELECT * FROM {P} ORDER BY id OFFSET {DEEP} LIMIT {PAGE}"),
        ),
        (
            "distinct",
            "filter(city).distinct()[:50]",
            lambda: list(C.filter(city="city42").distinct()[:PAGE]),
            raw(WideCustomerU7, f"SELECT DISTINCT * FROM {P} WHERE city = 'city42' LIMIT {PAGE}"),
        ),
        (
            "aggregate",
            "filter(city).aggregate(Avg(score))",
            lambda: C.filter(city="city42").aggregate(a=Avg("score"))["a"],
            raw(WideCustomerU7, f"SELECT avg(score) AS id FROM {P} WHERE city = 'city42'"),
        ),
        (
            "join",
            "VIEW->PLAIN  customer.region",
            lambda: list(C.filter(region__name="region7")[:PAGE]),
            raw(
                WideCustomerU7,
                f"SELECT c.* FROM {P} c JOIN testapp_wideregion r ON r.id = c.region_id "
                f"WHERE r.name = 'region7' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "PLAIN->VIEW  note.customer",
            lambda: list(WideCustomerNoteU7.objects.filter(customer__city="city42")[:PAGE]),
            raw(
                WideCustomerNoteU7,
                f"SELECT n.* FROM testapp_widecustomernoteu7 n "
                f"JOIN {P} c ON c.id = n.customer_id WHERE c.city = 'city42' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "VIEW->VIEW   order.customer  2 views",
            lambda: list(WideOrderU7.objects.filter(customer__city="city42")[:PAGE]),
            raw(
                WideOrderU7,
                f"SELECT o.* FROM bu7_order o JOIN {P} c ON c.id = o.customer_id WHERE c.city = 'city42' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "VIEW->VIEW   line.product    2 views",
            lambda: list(WideOrderLineU7.objects.filter(product__category="cat7")[:PAGE]),
            raw(
                WideOrderLineU7,
                f"SELECT l.* FROM bu7_line l JOIN bu7_product p ON p.id = l.product_id "
                f"WHERE p.category = 'cat7' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "VIEW->VIEW->VIEW  line.order.customer  3 views",
            lambda: list(WideOrderLineU7.objects.filter(order__customer__city="city42")[:PAGE]),
            raw(
                WideOrderLineU7,
                f"SELECT l.* FROM bu7_line l JOIN bu7_order o ON o.id = l.order_id "
                f"JOIN {P} c ON c.id = o.customer_id WHERE c.city = 'city42' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "3 views + plain   ….region.name",
            lambda: list(WideOrderLineU7.objects.filter(order__customer__region__name="region7")[:PAGE]),
            raw(
                WideOrderLineU7,
                f"SELECT l.* FROM bu7_line l JOIN bu7_order o ON o.id = l.order_id "
                f"JOIN {P} c ON c.id = o.customer_id "
                f"JOIN testapp_wideregion r ON r.id = c.region_id WHERE r.name = 'region7' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "4 views           line.product + line.order.customer",
            lambda: list(
                WideOrderLineU7.objects.filter(product__category="cat7", order__customer__status="active")[:PAGE]
            ),
            raw(
                WideOrderLineU7,
                f"SELECT l.* FROM bu7_line l JOIN bu7_order o ON o.id = l.order_id "
                f"JOIN {P} c ON c.id = o.customer_id JOIN bu7_product p ON p.id = l.product_id "
                f"WHERE p.category = 'cat7' AND c.status = 'active' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "select_related('order__customer')",
            lambda: list(WideOrderLineU7.objects.select_related("order__customer").order_by("id")[:PAGE]),
            raw(
                WideOrderLineU7,
                f"SELECT l.* FROM bu7_line l JOIN bu7_order o ON o.id = l.order_id "
                f"JOIN {P} c ON c.id = o.customer_id ORDER BY l.id LIMIT {PAGE}",
            ),
        ),
        # --- the same traversals, done the way application code could do them
        # instead. A view-to-view join is the shape that falls over; a pk IN
        # (...) lookup is the shape the view is fastest at.
        (
            "rewrite",
            "prefetch_related('order__customer')  vs select_related",
            lambda: list(WideOrderLineU7.objects.prefetch_related("order__customer").order_by("id")[:PAGE]),
            raw(
                WideOrderLineU7,
                f"SELECT l.* FROM bu7_line l JOIN bu7_order o ON o.id = l.order_id "
                f"JOIN {P} c ON c.id = o.customer_id ORDER BY l.id LIMIT {PAGE}",
            ),
        ),
        (
            "rewrite",
            "3 views as 3 queries with IN lists",
            lambda: _three_queries(),
            raw(
                WideOrderLineU7,
                f"SELECT l.* FROM bu7_line l JOIN bu7_order o ON o.id = l.order_id "
                f"JOIN {P} c ON c.id = o.customer_id WHERE c.city = 'city42' LIMIT {PAGE}",
            ),
        ),
        (
            "join",
            "annotate(Count('orders'))",
            lambda: list(C.filter(city="city42").annotate(c=Count("orders")).order_by("id")[:PAGE]),
            raw(
                WideCustomerU7,
                f"SELECT c.*, count(o.id) AS c2 FROM {P} c LEFT JOIN bu7_order o ON o.customer_id = c.id "
                f"WHERE c.city = 'city42' GROUP BY c.id ORDER BY c.id LIMIT {PAGE}",
            ),
        ),
    ]


def test_uuid7_wide_scale():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s")

    rows = scalar("SELECT count(*) FROM widecustomer_u7_view")
    print(
        f"widecustomer_u7_view: {rows:,} rows   (strategy: UUID7_POLYFILL)   "
        f"source indexes: {'MIRRORED' if INDEX_SOURCES else 'NONE (pk only)'}\n"
    )

    vendor_pk, override_pk, organic_pk = pks()

    header = f"{'':<48} {'overlay':>11} {'plain':>11} {'ratio':>9}   rows"
    print(header)
    print("-" * len(header))
    group = None
    for case_group, label, overlay, plain in cases(vendor_pk, override_pk, organic_pk):
        if case_group != group:
            group = case_group
            print(f"\n{case_group.upper()}")
        overlay_ms, produced, timed_out = best_of(overlay)
        plain_ms, plain_produced, plain_timed_out = best_of(plain)
        if not timed_out:
            assert n(produced), f"{label!r} measured an empty query"
        if not plain_timed_out:
            assert n(plain_produced), f"{label!r} plain baseline measured an empty query"

        overlay_text = f">{TIMEOUT_MS / 1000:.0f}s" if timed_out else f"{overlay_ms:.1f}ms"
        plain_text = f">{TIMEOUT_MS / 1000:.0f}s" if plain_timed_out else f"{plain_ms:.1f}ms"
        if timed_out or plain_timed_out or plain_ms == 0:
            ratio = "-"
        else:
            ratio = f"{overlay_ms / plain_ms:.1f}x"
        print(f"  {label:<46} {overlay_text:>11} {plain_text:>11} {ratio:>9}   {n(produced)}")
