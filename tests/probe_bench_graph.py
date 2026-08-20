"""The production-shaped graph: overridable entities, append-only link tables.

    BenchPerson, BenchAddress, BenchPhone, BenchEmail
        overridable = True, soft_delete = True     -> full anti-join + a qual
                                                      on the base branch

    BenchPersonAddress, BenchPersonPhone, BenchPersonEmail
        overridable = False, soft_delete = False   -> bare UNION ALL

TODO/20 measured those two shapes in isolation and found a 55ms / 0.029ms gap
between them on ordered paging. This graph puts one of each on either side of
every join, which is the arrangement the application actually runs, and the
question is what happens where they meet.

Every comparison is against a plain, non-overlay table built from the view
itself, carrying the same indexes -- so the ratio column is like-for-like.

    OVERLAY_BENCH_SCALE=0.3 POSTGRES_USER=postgres uv run pytest \\
        tests/probe_bench_graph.py -s -q -o addopts="" --no-cov

    OVERLAY_BENCH_SCALE=0.05 ...      # quick iteration
    OVERLAY_BENCH_SHARE=0.1 ...       # base holds 10% of the view instead of 40%
"""

import os
import time
from contextlib import contextmanager

import pytest
from django.db import OperationalError, connection

from tests.probe_uuid7_scale import u


pytestmark = pytest.mark.django_db(transaction=True)

SCALE = float(os.environ.get("OVERLAY_BENCH_SCALE", 0.3))
SHARE = float(os.environ.get("OVERLAY_BENCH_SHARE", 0.4))
TIMEOUT_MS = int(os.environ.get("OVERLAY_BENCH_TIMEOUT_MS", 30_000))


def scaled(n):
    return max(2, int(n * SCALE))


PERSON_VIEW = scaled(1_000_000)
ADDRESS_VIEW = scaled(800_000)
PHONE_VIEW = scaled(700_000)
EMAIL_VIEW = scaled(600_000)
PA_VIEW = scaled(1_500_000)
PP_VIEW = scaled(1_200_000)
PE_VIEW = scaled(1_000_000)
# The tenant-only entity. Not scaled: a tenant has tens to low hundreds of
# labels however many people it holds, and holding it fixed keeps each label's
# selectivity comparable as SCALE moves.
LABEL_COUNT = 200
# Three labels per person rather than one. At one, almost nobody carries two
# labels and every "has label A and label B" case measures an empty result --
# which times how fast Postgres finds nothing, not how fast it intersects.
PL_ROWS = scaled(3_000_000)

ORGANIC_OFFSET = 100_000_000

ENTITIES = {
    "person": ("bench_person", "testapp_shared_benchpersonsource", PERSON_VIEW),
    "address": ("bench_address", "testapp_shared_benchaddresssource", ADDRESS_VIEW),
    "phone": ("bench_phone", "testapp_shared_benchphonesource", PHONE_VIEW),
    "email": ("bench_email", "testapp_shared_benchemailsource", EMAIL_VIEW),
}
LINKS = {
    "person_address": ("bench_person_address", "testapp_shared_benchpersonaddresssource", PA_VIEW),
    "person_phone": ("bench_person_phone", "testapp_shared_benchpersonphonesource", PP_VIEW),
    "person_email": ("bench_person_email", "testapp_shared_benchpersonemailsource", PE_VIEW),
}

PLAIN = {name: f"plain_{name}" for name in list(ENTITIES) + list(LINKS)}

# Tenant-owned outright: no source table, no view, no overlay machinery. Listed
# separately because every other collection here is keyed by a base/source
# pair and these have no source half to pair with.
TENANT_ONLY = ("bench_label", "bench_person_label", "plain_person_label")

# The overlay view exposes the model's own columns; the plain mirror's table has
# the same ones. Listed explicitly rather than `SELECT *` because the view's
# column order is the select list's, not the table's.
PLAIN_COLUMNS = {
    "person": ("id", "first_name", "last_name", "city", "postcode", "status", "score",
               "born_on", "notes"),
    "address": ("id", "line1", "city", "postcode", "country"),
    "phone": ("id", "number", "kind"),
    "email": ("id", "address", "domain", "kind"),
    "person_address": ("id", "person_id", "address_id", "role"),
    "person_phone": ("id", "person_id", "phone_id", "role"),
    "person_email": ("id", "person_id", "email_id", "role"),
}


def split(view_rows, overridable):
    """(source rows, override rows, organic rows) for a target view size.

    An override shares its source row's id, so it does not add a view row; an
    organic row does. A link table cannot be overridden at all, so its base
    half is entirely organic."""
    base = int(view_rows * SHARE)
    overrides = base // 2 if overridable else 0
    organic = base - overrides
    return view_rows - organic, overrides, organic


CACHE_SCHEMA = "bench_cache"
# Bumped whenever the set of loaded tables changes. Without it a cache that
# predates a new table matches on scale/share alone and restores a graph with
# that table silently empty -- which reads as "the feature found nothing"
# rather than "the cache is stale".
CACHE_VERSION = 3


def sql(statement, *params):
    with connection.cursor() as cursor:
        cursor.execute(statement, params or None)


@contextmanager
def indexes_dropped(tables):
    """Bulk-load with the indexes off, then put them back.

    This is why the load went from 25s to 120s when the plain mirrors stopped
    being `CREATE TABLE AS` and became Django models: inserting a few million
    rows into a table that already carries six indexes maintains all six per
    row. Building them once at the end is far cheaper.

    Constraint-backed indexes are left alone -- DROP INDEX refuses them, and
    they are the primary keys and unique constraints the load depends on.
    """
    saved = []
    for table in tables:
        for name, definition in rows(
            "SELECT i.indexname, i.indexdef FROM pg_indexes i "
            f"WHERE i.schemaname = 'public' AND i.tablename = '{table}' "  # noqa: S608
            "AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conname = i.indexname)"
        ):
            saved.append((name, definition))
            sql(f"DROP INDEX {name}")
    try:
        yield
    finally:
        for _, definition in saved:
            sql(definition)


def _cache_is_current() -> bool:
    stamped = rows(
        f"SELECT to_regclass('{CACHE_SCHEMA}.stamp') IS NOT NULL"  # noqa: S608
    )[0][0]
    if not stamped:
        return False
    match = rows(
        f"SELECT count(*) FROM {CACHE_SCHEMA}.stamp "  # noqa: S608
        f"WHERE scale = {SCALE} AND share = {SHARE} AND version = {CACHE_VERSION}"
    )[0][0]
    return bool(match)


def _all_tables():
    return (
        [base for base, _, _ in list(ENTITIES.values()) + list(LINKS.values())]
        + [source for _, source, _ in list(ENTITIES.values()) + list(LINKS.values())]
        + list(PLAIN.values())
        + list(TENANT_ONLY)
    )


def _fill_cache():
    """Snapshot every loaded table into a schema Django does not know about.

    `django_db(transaction=True)` flushes on teardown, and the flush only
    targets tables in the app registry -- so a separate schema survives
    between runs, which `--reuse-db` then makes worth having."""
    sql(f"DROP SCHEMA IF EXISTS {CACHE_SCHEMA} CASCADE")
    sql(f"CREATE SCHEMA {CACHE_SCHEMA}")
    for table in _all_tables():
        sql(f"CREATE TABLE {CACHE_SCHEMA}.{table} AS TABLE public.{table}")  # noqa: S608
    sql(f"CREATE TABLE {CACHE_SCHEMA}.stamp "
        f"(scale double precision, share double precision, version integer)")
    sql(f"INSERT INTO {CACHE_SCHEMA}.stamp VALUES ({SCALE}, {SHARE}, {CACHE_VERSION})")  # noqa: S608


def _restore_from_cache() -> bool:
    if not _cache_is_current():
        return False
    tables = _all_tables()
    sql("SET CONSTRAINTS ALL IMMEDIATE")
    sql(f"TRUNCATE {', '.join(tables)} CASCADE")
    sql("SET CONSTRAINTS ALL DEFERRED")
    for table in tables:
        sql(f"ALTER TABLE {table} DISABLE TRIGGER USER")
    with indexes_dropped(tables):
        for table in tables:
            sql(f"INSERT INTO public.{table} TABLE {CACHE_SCHEMA}.{table}")  # noqa: S608
    for table in tables:
        sql(f"ALTER TABLE {table} ENABLE TRIGGER USER")
        sql(f"ANALYZE {table}")
    return True


def rows(statement):
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return cursor.fetchall()


def scalar(statement):
    return rows(statement)[0][0]


def plan(statement):
    return [row[0] for row in rows("EXPLAIN (ANALYZE, BUFFERS) " + statement)]


def best_of(statement, rounds=3, give_up_after_ms=3000):
    """(milliseconds, timed_out)."""
    best = None
    for _ in range(rounds):
        sql(f"SET statement_timeout = {TIMEOUT_MS}")
        started = time.perf_counter()
        try:
            rows(statement)
        except OperationalError:
            sql("SET statement_timeout = 0")
            return float(TIMEOUT_MS), True
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
        if best > give_up_after_ms:
            break
    sql("SET statement_timeout = 0")
    return best, False


def shape_of(lines):
    joined = "\n".join(lines)
    if "Merge Append" in joined and "Sort Method" not in joined:
        return "MergeAppend"
    if "Nested Loop" in joined and "Materialize" in joined:
        return "NestLoop+Mat"
    if "Sort Method" in joined:
        return "sort"
    if "Merge Append" in joined:
        return "MergeAppend+sort"
    return "append"


# --------------------------------------------------------------------- load


def load():
    """Build the graph, or restore it from the cache schema if one matches.

    Set OVERLAY_BENCH_REBUILD=1 to force a rebuild. Combine with pytest-django's
    --reuse-db to make the cache worth having across processes:

        OVERLAY_BENCH_SCALE=0.3 POSTGRES_USER=postgres uv run pytest \\
            --reuse-db tests/probe_orm_benchmark.py -s -q -o addopts="" --no-cov
    """
    if os.environ.get("OVERLAY_BENCH_REBUILD") != "1" and _restore_from_cache():
        return

    tables = [base for base, _, _ in list(ENTITIES.values()) + list(LINKS.values())]
    sources = [source for _, source, _ in list(ENTITIES.values()) + list(LINKS.values())]

    sql("SET CONSTRAINTS ALL IMMEDIATE")
    sql(f"TRUNCATE {', '.join(tables + sources + list(PLAIN.values()) + list(TENANT_ONLY))} CASCADE")
    sql("SET CONSTRAINTS ALL DEFERRED")
    for table in tables:
        sql(f"ALTER TABLE {table} DISABLE TRIGGER USER")

    person_source, person_override, person_organic = split(PERSON_VIEW, True)
    address_source, address_override, address_organic = split(ADDRESS_VIEW, True)
    phone_source, phone_override, phone_organic = split(PHONE_VIEW, True)
    email_source, email_override, email_organic = split(EMAIL_VIEW, True)

    # ------------------------------------------------------------- entities
    person_columns = "first_name, last_name, city, postcode, status, score, born_on, notes"
    person_values = (
        "'first' || g, 'last' || (g %% 5000), 'city' || (g %% 1000), 'pc' || (g %% 9999), "
        "(ARRAY['active','lapsed','pending','closed'])[1 + g %% 4], g %% 1000, "
        "DATE '1950-01-01' + (g %% 25000), ''"
    )
    for table, ids, count in (
        ("testapp_shared_benchpersonsource", u("g"), person_source),
        ("bench_person", u("g"), person_override),
        ("bench_person", u(f"(g + {ORGANIC_OFFSET})"), person_organic),
    ):
        deleted = ", _overlay_deleted" if table == "bench_person" else ""
        value = ", FALSE" if table == "bench_person" else ""
        sql(
            f"INSERT INTO {table} (id, {person_columns}{deleted}) "
            f"SELECT {ids}, {person_values}{value} FROM generate_series(1, %s) g",
            count,
        )

    address_columns = "line1, city, postcode, country"
    address_values = (
        "g || ' Example Street', 'city' || (g %% 1000), 'pc' || (g %% 9999), "
        "(ARRAY['GB','US','FR','DE'])[1 + g %% 4]"
    )
    for table, ids, count in (
        ("testapp_shared_benchaddresssource", u("g"), address_source),
        ("bench_address", u("g"), address_override),
        ("bench_address", u(f"(g + {ORGANIC_OFFSET})"), address_organic),
    ):
        deleted = ", _overlay_deleted" if table == "bench_address" else ""
        value = ", FALSE" if table == "bench_address" else ""
        sql(
            f"INSERT INTO {table} (id, {address_columns}{deleted}) "
            f"SELECT {ids}, {address_values}{value} FROM generate_series(1, %s) g",
            count,
        )

    phone_columns = "number, kind"
    phone_values = "'+4470' || lpad(g::text, 8, '0'), (ARRAY['mobile','home','work'])[1 + g %% 3]"
    for table, ids, count in (
        ("testapp_shared_benchphonesource", u("g"), phone_source),
        ("bench_phone", u("g"), phone_override),
        ("bench_phone", u(f"(g + {ORGANIC_OFFSET})"), phone_organic),
    ):
        deleted = ", _overlay_deleted" if table == "bench_phone" else ""
        value = ", FALSE" if table == "bench_phone" else ""
        sql(
            f"INSERT INTO {table} (id, {phone_columns}{deleted}) "
            f"SELECT {ids}, {phone_values}{value} FROM generate_series(1, %s) g",
            count,
        )

    email_columns = "address, domain, kind"
    email_values = (
        "'user' || g || '@' || (ARRAY['example.com','mail.test','corp.example'])[1 + g %% 3], "
        "(ARRAY['example.com','mail.test','corp.example'])[1 + g %% 3], "
        "(ARRAY['primary','work','other'])[1 + g %% 3]"
    )
    for table, ids, count in (
        ("testapp_shared_benchemailsource", u("g"), email_source),
        ("bench_email", u("g"), email_override),
        ("bench_email", u(f"(g + {ORGANIC_OFFSET})"), email_organic),
    ):
        deleted = ", _overlay_deleted" if table == "bench_email" else ""
        value = ", FALSE" if table == "bench_email" else ""
        sql(
            f"INSERT INTO {table} (id, {email_columns}{deleted}) "
            f"SELECT {ids}, {email_values}{value} FROM generate_series(1, %s) g",
            count,
        )

    # ---------------------------------------------------------------- links
    # Half the links point at a vendor-backed row and half at an organic one,
    # so every traversal has to cross both branches of the target view.
    def reference(source_count, organic_count):
        return (
            f"CASE WHEN g %% 2 = 0 THEN {u(f'(1 + g %% {source_count})')} "
            f"ELSE {u(f'({ORGANIC_OFFSET} + 1 + g %% {max(organic_count, 1)})')} END"
        )

    person_ref = reference(person_source, person_organic)
    for link, target_column, target_source, target_organic, roles, total in (
        ("person_address", "address_id", address_source, address_organic,
         "(ARRAY['home','work','billing'])[1 + g %% 3]", PA_VIEW),
        ("person_phone", "phone_id", phone_source, phone_organic,
         "(ARRAY['mobile','home','work'])[1 + g %% 3]", PP_VIEW),
        ("person_email", "email_id", email_source, email_organic,
         "(ARRAY['primary','work','other'])[1 + g %% 3]", PE_VIEW),
    ):
        base, source, _ = LINKS[link]
        link_source, _, link_organic = split(total, False)
        values = f"{person_ref}, {reference(target_source, target_organic)}, {roles}"
        columns = f"person_id, {target_column}, role"
        sql(
            f"INSERT INTO {source} (id, {columns}) "
            f"SELECT {u('g')}, {values} FROM generate_series(1, %s) g",
            link_source,
        )
        sql(
            f"INSERT INTO {base} (id, {columns}) "
            f"SELECT {u(f'(g + {ORGANIC_OFFSET})')}, {values} "
            f"FROM generate_series(1, %s) g",
            link_organic,
        )

    # ------------------------------------------------- the tenant-only entity
    # BenchLabel has no source table and no view: it is an ordinary table, the
    # way a label or a saved list is tenant-owned outright with no vendor row
    # to merge. Its two link tables are ordinary too. This is the "don't
    # overlay what you join through" arrangement, and the reason it is here is
    # that a join from the person *view* to a plain table is the one shape the
    # rest of this graph cannot produce.
    #
    # Both link tables are filled from the same expression, so the overlay and
    # plain sides of the comparison describe the same graph over the same
    # people.
    sql(
        "INSERT INTO bench_label (id, name, kind) "
        "SELECT g, 'label' || g, (ARRAY['volunteer','donor','lapsed','vip'])[1 + g %% 4] "
        "FROM generate_series(1, %s) g",
        LABEL_COUNT,
    )
    # Only the overlay-side link here. `plain_person_label.person` is a real
    # ForeignKey to plain_person, which is not filled until the mirrors below,
    # so its rows have to wait -- see the second half of this pair further
    # down. The overlay side has no such ordering constraint: its person column
    # is an OverlayForeignKey, which carries no database-level FK because
    # Postgres cannot constrain against a view.
    sql(
        f"INSERT INTO bench_person_label (id, person_id, label_id) "
        f"SELECT {u('g')}, {person_ref}, 1 + g %% {LABEL_COUNT} "
        f"FROM generate_series(1, %s) g",
        PL_ROWS,
    )

    for table in tables:
        sql(f"ALTER TABLE {table} ENABLE TRIGGER USER")

    # ------------------------------------------- plain mirrors of each view
    # Django models now own these tables (PlainPerson and friends), so the
    # benchmark can compare ORM against ORM rather than ORM against raw SQL.
    # Filled from the views, so the contents are identical by construction.
    #
    # Indexes off for the insert: these tables carry six each, and maintaining
    # them per row is what took the load from 25s to 120s.
    with indexes_dropped(list(PLAIN.values())):
        for name, columns in PLAIN_COLUMNS.items():
            base = (ENTITIES | LINKS)[name][0]
            selected = ", ".join(columns)
            sql(f"INSERT INTO {PLAIN[name]} ({selected}) SELECT {selected} FROM {base}_view")  # noqa: S608

    # The other half of the label pair. plain_person exists now, so this side's
    # real ForeignKey can finally be satisfied. Copied from the overlay link
    # rather than regenerated, so the two describe exactly the same graph even
    # if the generating expression above ever changes.
    with indexes_dropped(["plain_person_label"]):
        sql(
            "INSERT INTO plain_person_label (id, person_id, label_id) "
            "SELECT id, person_id, label_id FROM bench_person_label"
        )

    for table in tables + sources + list(PLAIN.values()) + list(TENANT_ONLY):
        sql(f"ANALYZE {table}")

    _fill_cache()


# ------------------------------------------------------------------ shapes


def report(label, view_sql, plain_sql, notes=""):
    view_ms, view_timeout = best_of(view_sql)
    plain_ms, _ = best_of(plain_sql)
    shape = shape_of(plan(view_sql)) if not view_timeout else "TIMEOUT"
    ratio = view_ms / plain_ms if plain_ms else float("nan")
    marker = ">" if view_timeout else " "
    print(f"  {label:<44} {marker}{view_ms:>9.1f}ms {plain_ms:>10.1f}ms  x{ratio:>9.2f}  "
          f"{shape:>15}  {notes}")
    return ratio


def test_bench_graph():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s   "
          f"(scale {SCALE}, base share {SHARE})")

    print("\n  " + "-" * 96)
    print(f"  {'relation':<26} {'view':>12} {'base':>12} {'source':>12}   shape")
    print("  " + "-" * 96)
    for name, (base, source, _) in (ENTITIES | LINKS).items():
        kind = "full anti-join + qual" if name in ENTITIES else "bare UNION ALL"
        print(f"  {name:<26} {scalar(f'SELECT count(*) FROM {base}_view'):>12,} "
              f"{scalar(f'SELECT count(*) FROM {base}'):>12,} "
              f"{scalar(f'SELECT count(*) FROM {source}'):>12,}   {kind}")

    header = (f"  {'query':<44} {'overlay':>11} {'plain':>11}  {'ratio':>10}  {'plan':>15}")

    print("\n" + "=" * 108)
    print("A. ENTITY SHAPE (overridable + soft_delete) -- the expensive half")
    print("=" * 108)
    print(header)
    print("  " + "-" * 104)
    report("point lookup by pk",
           "SELECT * FROM bench_person_view WHERE id = "
           "(SELECT id FROM bench_person_view LIMIT 1)",
           f"SELECT * FROM {PLAIN['person']} WHERE id = "
           f"(SELECT id FROM {PLAIN['person']} LIMIT 1)")
    report("equality on indexed column (city)",
           "SELECT * FROM bench_person_view WHERE city = 'city42'",
           f"SELECT * FROM {PLAIN['person']} WHERE city = 'city42'")
    report("equality on UNindexed column (born_on)",
           "SELECT * FROM bench_person_view WHERE born_on = DATE '1970-01-01'",
           f"SELECT * FROM {PLAIN['person']} WHERE born_on = DATE '1970-01-01'")
    report("scoped + ordered (city, score DESC)",
           "SELECT * FROM bench_person_view WHERE city = 'city42' ORDER BY score DESC LIMIT 20",
           f"SELECT * FROM {PLAIN['person']} WHERE city = 'city42' ORDER BY score DESC LIMIT 20")
    report("UNSCOPED ordered page",
           "SELECT * FROM bench_person_view ORDER BY score DESC LIMIT 20",
           f"SELECT * FROM {PLAIN['person']} ORDER BY score DESC LIMIT 20")
    report("UNSCOPED ordered, deep offset",
           "SELECT * FROM bench_person_view ORDER BY score DESC LIMIT 20 OFFSET 100000",
           f"SELECT * FROM {PLAIN['person']} ORDER BY score DESC LIMIT 20 OFFSET 100000")
    report("count(*)",
           "SELECT count(*) FROM bench_person_view",
           f"SELECT count(*) FROM {PLAIN['person']}")

    print("\n" + "=" * 108)
    print("B. LINK SHAPE (overridable=False, soft_delete=False) -- the cheap half")
    print("=" * 108)
    print(header)
    print("  " + "-" * 104)
    report("person_id lookup",
           "SELECT * FROM bench_person_address_view WHERE person_id = "
           "(SELECT id FROM bench_person_view LIMIT 1)",
           f"SELECT * FROM {PLAIN['person_address']} WHERE person_id = "
           f"(SELECT id FROM {PLAIN['person']} LIMIT 1)")
    report("UNSCOPED ordered page",
           "SELECT * FROM bench_person_address_view ORDER BY person_id LIMIT 20",
           f"SELECT * FROM {PLAIN['person_address']} ORDER BY person_id LIMIT 20")
    report("UNSCOPED ordered, deep offset",
           "SELECT * FROM bench_person_address_view ORDER BY person_id LIMIT 20 OFFSET 100000",
           f"SELECT * FROM {PLAIN['person_address']} ORDER BY person_id LIMIT 20 OFFSET 100000")
    report("count(*)",
           "SELECT count(*) FROM bench_person_address_view",
           f"SELECT count(*) FROM {PLAIN['person_address']}")

    print("\n" + "=" * 108)
    print("C. WHERE THE TWO MEET -- joins across the shape boundary")
    print("=" * 108)
    print(header)
    print("  " + "-" * 104)
    one_person = "(SELECT id FROM bench_person_view LIMIT 1)"
    one_plain = f"(SELECT id FROM {PLAIN['person']} LIMIT 1)"
    report("detail: one person -> addresses",
           "SELECT a.* FROM bench_address_view a "
           "JOIN bench_person_address_view l ON l.address_id = a.id "
           f"WHERE l.person_id = {one_person}",
           f"SELECT a.* FROM {PLAIN['address']} a "
           f"JOIN {PLAIN['person_address']} l ON l.address_id = a.id "
           f"WHERE l.person_id = {one_plain}")
    report("detail: one person -> all three relations",
           "SELECT a.id, p.id, e.id FROM bench_person_view person "
           "LEFT JOIN bench_person_address_view la ON la.person_id = person.id "
           "LEFT JOIN bench_address_view a ON a.id = la.address_id "
           "LEFT JOIN bench_person_phone_view lp ON lp.person_id = person.id "
           "LEFT JOIN bench_phone_view p ON p.id = lp.phone_id "
           "LEFT JOIN bench_person_email_view le ON le.person_id = person.id "
           "LEFT JOIN bench_email_view e ON e.id = le.email_id "
           f"WHERE person.id = {one_person}",
           f"SELECT a.id, p.id, e.id FROM {PLAIN['person']} person "
           f"LEFT JOIN {PLAIN['person_address']} la ON la.person_id = person.id "
           f"LEFT JOIN {PLAIN['address']} a ON a.id = la.address_id "
           f"LEFT JOIN {PLAIN['person_phone']} lp ON lp.person_id = person.id "
           f"LEFT JOIN {PLAIN['phone']} p ON p.id = lp.phone_id "
           f"LEFT JOIN {PLAIN['person_email']} le ON le.person_id = person.id "
           f"LEFT JOIN {PLAIN['email']} e ON e.id = le.email_id "
           f"WHERE person.id = {one_plain}")
    report("reverse: people at addresses in a city",
           "SELECT DISTINCT person.id FROM bench_person_view person "
           "JOIN bench_person_address_view l ON l.person_id = person.id "
           "JOIN bench_address_view a ON a.id = l.address_id "
           "WHERE a.city = 'city42' LIMIT 50",
           f"SELECT DISTINCT person.id FROM {PLAIN['person']} person "
           f"JOIN {PLAIN['person_address']} l ON l.person_id = person.id "
           f"JOIN {PLAIN['address']} a ON a.id = l.address_id "
           f"WHERE a.city = 'city42' LIMIT 50")
    report("reverse, rewritten as = ANY (ARRAY ...)",
           "SELECT person.id FROM bench_person_view person WHERE person.id = ANY (ARRAY("
           "SELECT l.person_id FROM bench_person_address_view l WHERE l.address_id = ANY (ARRAY("
           "SELECT a.id FROM bench_address_view a WHERE a.city = 'city42')))) LIMIT 50",
           f"SELECT person.id FROM {PLAIN['person']} person WHERE person.id = ANY (ARRAY("
           f"SELECT l.person_id FROM {PLAIN['person_address']} l WHERE l.address_id = ANY (ARRAY("
           f"SELECT a.id FROM {PLAIN['address']} a WHERE a.city = 'city42')))) LIMIT 50")
    report("reverse: people with a phone number",
           "SELECT person.id FROM bench_person_view person "
           "JOIN bench_person_phone_view l ON l.person_id = person.id "
           "JOIN bench_phone_view p ON p.id = l.phone_id "
           "WHERE p.number = '+447000000042' LIMIT 50",
           f"SELECT person.id FROM {PLAIN['person']} person "
           f"JOIN {PLAIN['person_phone']} l ON l.person_id = person.id "
           f"JOIN {PLAIN['phone']} p ON p.id = l.phone_id "
           f"WHERE p.number = '+447000000042' LIMIT 50")
    report("list page: 20 people + their address count",
           "SELECT person.id, count(l.id) FROM bench_person_view person "
           "LEFT JOIN bench_person_address_view l ON l.person_id = person.id "
           "WHERE person.city = 'city42' GROUP BY person.id ORDER BY person.id LIMIT 20",
           f"SELECT person.id, count(l.id) FROM {PLAIN['person']} person "
           f"LEFT JOIN {PLAIN['person_address']} l ON l.person_id = person.id "
           f"WHERE person.city = 'city42' GROUP BY person.id ORDER BY person.id LIMIT 20")

    print("\n" + "=" * 108)
    print("D. THE DETAIL PAGE, THREE WAYS -- can the 100x be written away?")
    print("=" * 108)
    print("  `person.addresses.all()` is the single most common query this schema will")
    print("  serve. OverlayQuery's rewrite covers forward FK traversals, not M2M ones,")
    print("  so this is whatever the ORM emits.")
    print(header)
    print("  " + "-" * 104)
    report("1. JOIN through the link view",
           "SELECT a.* FROM bench_address_view a "
           "JOIN bench_person_address_view l ON l.address_id = a.id "
           f"WHERE l.person_id = {one_person}",
           f"SELECT a.* FROM {PLAIN['address']} a "
           f"JOIN {PLAIN['person_address']} l ON l.address_id = a.id "
           f"WHERE l.person_id = {one_plain}")
    report("2. = ANY (ARRAY (subquery))",
           "SELECT a.* FROM bench_address_view a WHERE a.id = ANY (ARRAY("
           "SELECT l.address_id FROM bench_person_address_view l "
           f"WHERE l.person_id = {one_person}))",
           f"SELECT a.* FROM {PLAIN['address']} a WHERE a.id = ANY (ARRAY("
           f"SELECT l.address_id FROM {PLAIN['person_address']} l "
           f"WHERE l.person_id = {one_plain}))")

    # What prefetch_related actually does: fetch the link rows, then fetch the
    # targets by a literal list of ids. No join, no correlated subquery.
    link_ids = [
        str(row[0]) for row in rows(
            "SELECT address_id FROM bench_person_address_view "
            f"WHERE person_id = {one_person}"
        )
    ] or ["00000000-0000-7000-8000-000000000000"]
    literals = ", ".join(f"'{value}'::uuid" for value in link_ids)
    report("3. two queries, literal id list (prefetch_related)",
           f"SELECT * FROM bench_address_view WHERE id IN ({literals})",
           f"SELECT * FROM {PLAIN['address']} WHERE id IN ({literals})",
           notes=f"{len(link_ids)} ids")

    print("\n  the worst cross-boundary case, rewritten:")
    report("phone lookup, JOIN form",
           "SELECT person.id FROM bench_person_view person "
           "JOIN bench_person_phone_view l ON l.person_id = person.id "
           "JOIN bench_phone_view p ON p.id = l.phone_id "
           "WHERE p.number = '+447000000042' LIMIT 50",
           f"SELECT person.id FROM {PLAIN['person']} person "
           f"JOIN {PLAIN['person_phone']} l ON l.person_id = person.id "
           f"JOIN {PLAIN['phone']} p ON p.id = l.phone_id "
           f"WHERE p.number = '+447000000042' LIMIT 50")
    report("phone lookup, = ANY (ARRAY ...)",
           "SELECT person.id FROM bench_person_view person WHERE person.id = ANY (ARRAY("
           "SELECT l.person_id FROM bench_person_phone_view l WHERE l.phone_id = ANY (ARRAY("
           "SELECT p.id FROM bench_phone_view p WHERE p.number = '+447000000042')))) LIMIT 50",
           f"SELECT person.id FROM {PLAIN['person']} person WHERE person.id = ANY (ARRAY("
           f"SELECT l.person_id FROM {PLAIN['person_phone']} l WHERE l.phone_id = ANY (ARRAY("
           f"SELECT p.id FROM {PLAIN['phone']} p WHERE p.number = '+447000000042')))) LIMIT 50")

    print("\n" + "=" * 108)
    print("full plan: reverse traversal, plain JOIN form")
    print("=" * 108)
    for line in plan(
        "SELECT DISTINCT person.id FROM bench_person_view person "
        "JOIN bench_person_address_view l ON l.person_id = person.id "
        "JOIN bench_address_view a ON a.id = l.address_id "
        "WHERE a.city = 'city42' LIMIT 50"
    )[:22]:
        print("   ", line[:150])
