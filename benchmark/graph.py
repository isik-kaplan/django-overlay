"""The production-shaped graph: overridable entities, append-only link tables.

    BenchPerson, BenchAddress, BenchPhone, BenchEmail
        overridable = True, soft_delete = True     -> full anti-join + a qual
                                                      on the base branch

    BenchPersonAddress, BenchPersonPhone, BenchPersonEmail
        overridable = False, soft_delete = False   -> bare UNION ALL

Measured in isolation, those two shapes showed a 55ms / 0.029ms gap
between them on ordered paging. This graph puts one of each on either side of
every join, which is the arrangement the application actually runs, and the
question is what happens where they meet.

Every comparison is against a plain, non-overlay table built from the view
itself, carrying the same indexes -- so the ratio column is like-for-like.

This module is the single source of truth for benchmark data. Both the CLI
(`django-overlay benchmark`) and the exploratory probes under `tests/` build
their graph from here, so the two cannot drift apart and report numbers about
different tables.

The Django models themselves stay in `tests/testapp/models.py`, where the
permanent test suite already depends on them. Nothing here defines a model;
this is the loader and the table map.
"""

import os
import time
from contextlib import contextmanager

from django.db import OperationalError, connection


# Defaults, overridable by environment for the probes and by configure() for
# the CLI. Read at import time so a probe that never calls configure() behaves
# exactly as it did before this module moved out of tests/.
SCALE = float(os.environ.get("OVERLAY_BENCH_SCALE", 0.3))
SHARE = float(os.environ.get("OVERLAY_BENCH_SHARE", 0.4))
TIMEOUT_MS = int(os.environ.get("OVERLAY_BENCH_TIMEOUT_MS", 30_000))

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


def scaled(n):
    return max(2, int(n * SCALE))


# Row counts, recomputed by configure(). Module-level rather than passed around
# because the probes read them directly and predate the CLI.
PERSON_VIEW = ADDRESS_VIEW = PHONE_VIEW = EMAIL_VIEW = 0
PA_VIEW = PP_VIEW = PE_VIEW = PL_ROWS = 0

# The tenant-only entity. Not scaled: a tenant has tens to low hundreds of
# labels however many people it holds, and holding it fixed keeps each label's
# selectivity comparable as SCALE moves.
LABEL_COUNT = 200


def _recompute():
    """Refresh every derived row count from the current SCALE.

    The counts are module-level because the probes read them directly and
    predate the CLI; configure() reruns this so a --scale flag reaches them.
    """
    global PERSON_VIEW, ADDRESS_VIEW, PHONE_VIEW, EMAIL_VIEW
    global PA_VIEW, PP_VIEW, PE_VIEW, PL_ROWS
    PERSON_VIEW = scaled(1_000_000)
    ADDRESS_VIEW = scaled(800_000)
    PHONE_VIEW = scaled(700_000)
    EMAIL_VIEW = scaled(600_000)
    PA_VIEW = scaled(1_500_000)
    PP_VIEW = scaled(1_200_000)
    PE_VIEW = scaled(1_000_000)
    # Three labels per person rather than one. At one, almost nobody carries
    # two labels and every "has label A and label B" case measures an empty
    # result -- which times how fast Postgres finds nothing, not how fast it
    # intersects.
    PL_ROWS = scaled(3_000_000)
    for name, size in (
        ("person", PERSON_VIEW),
        ("address", ADDRESS_VIEW),
        ("phone", PHONE_VIEW),
        ("email", EMAIL_VIEW),
    ):
        base, source, _ = ENTITIES[name]
        ENTITIES[name] = (base, source, size)
    for name, size in (
        ("person_address", PA_VIEW),
        ("person_phone", PP_VIEW),
        ("person_email", PE_VIEW),
    ):
        base, source, _ = LINKS[name]
        LINKS[name] = (base, source, size)


def configure(scale=None, share=None, timeout_ms=None):
    """Point the loader at a different scale before calling load().

    The CLI calls this from its --scale flag; the probes let the environment
    defaults stand.
    """
    global SCALE, SHARE, TIMEOUT_MS
    if scale is not None:
        SCALE = float(scale)
    if share is not None:
        SHARE = float(share)
    if timeout_ms is not None:
        TIMEOUT_MS = int(timeout_ms)
    _recompute()


ENTITIES = {
    "person": ("bench_person", "testapp_shared_benchpersonsource", 0),
    "address": ("bench_address", "testapp_shared_benchaddresssource", 0),
    "phone": ("bench_phone", "testapp_shared_benchphonesource", 0),
    "email": ("bench_email", "testapp_shared_benchemailsource", 0),
}
LINKS = {
    "person_address": ("bench_person_address", "testapp_shared_benchpersonaddresssource", 0),
    "person_phone": ("bench_person_phone", "testapp_shared_benchpersonphonesource", 0),
    "person_email": ("bench_person_email", "testapp_shared_benchpersonemailsource", 0),
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
    "person": ("id", "first_name", "last_name", "city", "postcode", "status", "score", "born_on", "notes"),
    "address": ("id", "line1", "city", "postcode", "country"),
    "phone": ("id", "number", "kind"),
    "email": ("id", "address", "domain", "kind"),
    "person_address": ("id", "person_id", "address_id", "role"),
    "person_phone": ("id", "person_id", "phone_id", "role"),
    "person_email": ("id", "person_id", "email_id", "role"),
}

_recompute()


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


def rows(statement):
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return cursor.fetchall()


def scalar(statement):
    return rows(statement)[0][0]


def plan(statement):
    return [row[0] for row in rows("EXPLAIN (ANALYZE, BUFFERS) " + statement)]


def best_of(statement, rounds=3, give_up_after_ms=3000):
    """(milliseconds, timed_out) for a raw SQL statement.

    The ORM-level equivalent lives in harness.py and behaves differently -- it
    discards a warm-up round first. This one is for the raw-SQL probes, which
    compare statements against each other rather than a feature against itself.

    The session's own timeout is put back afterwards rather than cleared. An
    earlier version reset it to 0, which meant that in a full CLI run every
    suite after `shapes` executed with no statement timeout at all -- caught
    when a query in the staged suite ran for three minutes against a ten-second
    cap. Whatever the caller had configured is none of this function's business.
    """
    previous = rows("SHOW statement_timeout")[0][0]
    best = None
    try:
        for _ in range(rounds):
            sql(f"SET statement_timeout = {TIMEOUT_MS}")
            started = time.perf_counter()
            try:
                rows(statement)
            except OperationalError:
                return float(TIMEOUT_MS), True
            elapsed = (time.perf_counter() - started) * 1000
            best = elapsed if best is None else min(best, elapsed)
            if best > give_up_after_ms:
                break
    finally:
        sql(f"SET statement_timeout = '{previous}'")
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


def cache_is_warm() -> bool:
    """Whether load() will restore rather than build.

    The CLI asks before it runs anything, because a cold build at scale 1.0 is
    the single largest term in the runtime estimate and the difference between
    a two-minute run and a twenty-minute one.
    """
    try:
        return _cache_is_current()
    except Exception:  # noqa: BLE001 - no database yet is simply "not warm"
        return False


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
    between runs, which `--reuse-db` then makes worth having. Under the CLI the
    database is not a pytest test database at all and nothing flushes it, but
    the cache still earns its keep: it survives `--rebuild` of the schema and
    makes a scale switch reversible without paying the build twice."""
    sql(f"DROP SCHEMA IF EXISTS {CACHE_SCHEMA} CASCADE")
    sql(f"CREATE SCHEMA {CACHE_SCHEMA}")
    for table in _all_tables():
        sql(f"CREATE TABLE {CACHE_SCHEMA}.{table} AS TABLE public.{table}")  # noqa: S608
    sql(f"CREATE TABLE {CACHE_SCHEMA}.stamp (scale double precision, share double precision, version integer)")
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


# --------------------------------------------------------------------- load


def load(rebuild=False, progress=None):
    """Build the graph, or restore it from the cache schema if one matches.

    Returns (seconds, built) -- `built` is True when the graph was generated
    from scratch and False when it came from the cache. The caller uses that to
    calibrate its runtime estimates against the machine it is actually on.

    Set OVERLAY_BENCH_REBUILD=1, or pass rebuild=True, to force a rebuild.
    Combine with pytest-django's --reuse-db to make the cache worth having
    across processes:

        OVERLAY_BENCH_SCALE=0.3 POSTGRES_USER=postgres uv run pytest \\
            --reuse-db tests/probe_orm_benchmark.py -s -q -o addopts="" --no-cov
    """
    say = progress or (lambda message: None)
    started = time.perf_counter()

    if not rebuild and os.environ.get("OVERLAY_BENCH_REBUILD") != "1":
        if _restore_from_cache():
            return time.perf_counter() - started, False

    say(f"building the graph at scale {SCALE} ({PERSON_VIEW:,} people) -- this is the slow part")

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
    say("  entities")
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
        "g || ' Example Street', 'city' || (g %% 1000), 'pc' || (g %% 9999), (ARRAY['GB','US','FR','DE'])[1 + g %% 4]"
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
    say("  link tables")

    def reference(source_count, organic_count):
        return (
            f"CASE WHEN g %% 2 = 0 THEN {u(f'(1 + g %% {source_count})')} "
            f"ELSE {u(f'({ORGANIC_OFFSET} + 1 + g %% {max(organic_count, 1)})')} END"
        )

    person_ref = reference(person_source, person_organic)
    for link, target_column, target_source, target_organic, roles, total in (
        (
            "person_address",
            "address_id",
            address_source,
            address_organic,
            "(ARRAY['home','work','billing'])[1 + g %% 3]",
            PA_VIEW,
        ),
        (
            "person_phone",
            "phone_id",
            phone_source,
            phone_organic,
            "(ARRAY['mobile','home','work'])[1 + g %% 3]",
            PP_VIEW,
        ),
        (
            "person_email",
            "email_id",
            email_source,
            email_organic,
            "(ARRAY['primary','work','other'])[1 + g %% 3]",
            PE_VIEW,
        ),
    ):
        base, source, _ = LINKS[link]
        link_source, _, link_organic = split(total, False)
        values = f"{person_ref}, {reference(target_source, target_organic)}, {roles}"
        columns = f"person_id, {target_column}, role"
        sql(
            f"INSERT INTO {source} (id, {columns}) SELECT {u('g')}, {values} FROM generate_series(1, %s) g",
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
    say("  labels")
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
    say("  plain mirrors")
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

    say("  analyze")
    for table in tables + sources + list(PLAIN.values()) + list(TENANT_ONLY):
        sql(f"ANALYZE {table}")

    _fill_cache()
    return time.perf_counter() - started, True
