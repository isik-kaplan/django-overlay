"""What declaring a source partitioned is worth, swept by partition count.

Every other suite measures a query somebody writes. This one measures the
queries *nobody* writes: the probes the generated triggers run, one per row
written, inside an INSERT or an UPDATE where no timing is ever printed. That
is why the feature exists and why it needs a benchmark of its own -- a probe
that fails to prune returns exactly the right rows, so the only evidence it is
happening at all is the clock.

The claim under test is the one PERFORMANCE.md makes: without the partition key
in its predicate a lookup on a partitioned parent is not one index scan but
`partitions` of them. If that is true the unpruned column should climb with the
partition count while the pruned column stays flat, and the plain unpartitioned
table should sit alongside the pruned one. If it is not true, the declaration
buys nothing and the four templates that carry it should be reverted.

Three probe shapes, because the generated triggers are not all the same query.
A point lookup by id is what the FK checks and the delete-side guard do. A
lookup by a non-key column is what an OverlayUniqueConstraint does on every
insert, and it is the one with no index help from the partition scheme at all.
The third is that same uniqueness probe with the key prepended -- the "scoped"
form the docs recommend, and the only one that can prune.

Self-contained: it builds its own tables rather than partitioning the shared
bench fixture, which would change every other suite's numbers to measure this
one. Nothing here makes the library adapt -- the key is a declaration, read off
the code, never probed for at runtime. See docs/development/BENCHMARKS.md.
"""

from django.db import connection

from benchmark import harness


NAME = "partitions"
TITLE = "Partitioned sources: what the key declaration prunes away"

COLUMNS = ("unpruned", "pruned", "gain", "plain", "rows")

# How many partitions to sweep. The point is the shape of the curve, not any
# one number: the cost of an unpruned probe should track this column.
PARTITION_COUNTS = (4, 16, 64)

# Rows in every table, at scale 1.0. Spread evenly across whatever partition
# count is being measured, so the per-partition size shrinks as the count grows
# -- which is exactly the real situation, and the reason an unpruned scan does
# not simply get n times slower for free.
ROWS_AT_FULL_SCALE = 1_000_000

# Probes per measured cell. A trigger runs one per row written, so this is
# "what the generated probes cost for an insert of this many rows" -- which is
# both the readable unit and the one that matters.
PROBES_PER_BATCH = 500

PARENT = "bench_partitioned"
FLAT = "bench_unpartitioned"


def _sql(statement, params=None):
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def _drop():
    _sql(f"DROP TABLE IF EXISTS {PARENT}")
    _sql(f"DROP TABLE IF EXISTS {FLAT}")


def build_tables(rows: int, partitions: int) -> None:
    """A partitioned parent and a flat table holding identical rows.

    Both get the same two indexes, and on the parent both are created on the
    parent so every partition really has them -- an index attached to only some
    partitions is a correctness question for `show_source_indexes`, not a cost
    question for this. The flat table is the floor: it says how much of the
    unpruned column is the partitioning and how much is just the row count.
    """
    _drop()
    _sql(
        f"CREATE TABLE {PARENT} (id bigint NOT NULL, bucket int NOT NULL, email text NOT NULL) "
        "PARTITION BY LIST (bucket)"
    )
    for bucket in range(partitions):
        _sql(f"CREATE TABLE {PARENT}_{bucket} PARTITION OF {PARENT} FOR VALUES IN ({bucket})")
    _sql(f"CREATE TABLE {FLAT} (id bigint NOT NULL, bucket int NOT NULL, email text NOT NULL)")

    for table in (PARENT, FLAT):
        _sql(
            f"INSERT INTO {table} (id, bucket, email) "
            "SELECT i, mod(i, %s), 'person' || i || '@example.com' FROM generate_series(1, %s) AS i",
            [partitions, rows],
        )
        # Built after the load, which is faster and is what any real
        # blue-green rebuild of a source table does anyway.
        _sql(f"CREATE INDEX ON {table} (id)")
        _sql(f"CREATE INDEX ON {table} (email)")
        _sql(f"ANALYZE {table}")


def define_probe(name: str, table: str, predicate: str, partitions: int) -> None:
    """A PL/pgSQL function that runs one generated probe `n` times.

    PL/pgSQL specifically, and not a single set-returning statement driving the
    probes from `generate_series`. That form was tried first and measures the
    wrong thing: one statement means one plan, the key stops being a constant
    for the execution, and Postgres cannot prune the way it prunes for a
    trigger -- which reported the pruned column as *slower* than the unpruned
    one at 64 partitions, the opposite of the truth.

    A trigger is PL/pgSQL running one query per row with the key in a variable,
    so that is what this is. It also keeps the measurement server-side: a
    client round trip per probe would be added to all three columns equally and
    would flatten the ratio the suite exists to show.
    """
    _sql(f"DROP FUNCTION IF EXISTS {name}(int)")
    _sql(
        f"""
        CREATE FUNCTION {name}(n int) RETURNS bigint AS $$
        DECLARE
            hits bigint := 0;
            i int;
        BEGIN
            FOR i IN 1..n LOOP
                IF EXISTS (SELECT 1 FROM {table} WHERE {predicate.format(partitions=partitions)}) THEN
                    hits := hits + 1;
                END IF;
            END LOOP;
            RETURN hits;
        END
        $$ LANGUAGE plpgsql;
        """
    )


def call(name: str):
    def build():
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {name}(%s)", [PROBES_PER_BATCH])  # noqa: S608
            return cursor.fetchone()[0]

    return build


def row(ctx, section, label, slug, rows, partitions, unpruned, pruned):
    """One comparison: the same probe unpruned, pruned, and against a flat
    table holding the same rows."""
    define_probe(f"bench_probe_{slug}_unpruned", PARENT, unpruned, partitions)
    define_probe(f"bench_probe_{slug}_pruned", PARENT, pruned, partitions)
    define_probe(f"bench_probe_{slug}_flat", FLAT, unpruned, partitions)

    without, without_hits = ctx.measure(call(f"bench_probe_{slug}_unpruned"))
    with_key, with_hits = ctx.measure(call(f"bench_probe_{slug}_pruned"))
    plain, plain_hits = ctx.measure(call(f"bench_probe_{slug}_flat"))

    # The three have to agree. A pruned probe that missed its row would look
    # like a spectacular win, and this is the only thing here that can fail.
    ctx.compare(f"{label}: pruned vs unpruned", without_hits, with_hits)
    ctx.compare(f"{label}: partitioned vs flat", plain_hits, without_hits)

    section.add(
        label,
        {"unpruned": without, "pruned": with_key, "plain": plain},
        gain=harness.gain(without, with_key),
        rows=f"{rows:,}",
    )


def run(ctx):
    rows = max(1_000, int(ROWS_AT_FULL_SCALE * ctx.scale))
    try:
        for partitions in PARTITION_COUNTS:
            if ctx.out_of_time():
                return
            build_tables(rows, partitions)
            section = harness.Section(
                f"{partitions} partitions, {rows:,} rows",
                COLUMNS,
                note=(
                    f"{PROBES_PER_BATCH} probes a cell -- one per row a trigger would fire on. "
                    "`pruned` adds the partition key to the same predicate; `plain` is the same rows, flat"
                ),
            )
            row(
                ctx,
                section,
                "point lookup by id (FK checks)",
                "id",
                rows,
                partitions,
                unpruned="id = i",
                pruned="id = i AND bucket = mod(i, {partitions})",
            )
            row(
                ctx,
                section,
                "lookup by email (uniqueness)",
                "email",
                rows,
                partitions,
                unpruned="email = 'person' || i || '@example.com'",
                pruned="email = 'person' || i || '@example.com' AND bucket = mod(i, {partitions})",
            )
            row(
                ctx,
                section,
                "miss by email (the insert path)",
                "miss",
                rows,
                partitions,
                # The case every insert of a *new* value takes, and the
                # expensive one: nothing matches, so there is no row to stop at
                # and every partition has to be visited before the answer is no.
                unpruned="email = 'absent' || i || '@example.com'",
                pruned="email = 'absent' || i || '@example.com' AND bucket = mod(i, {partitions})",
            )
            yield section
    finally:
        _drop()
