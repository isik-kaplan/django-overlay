from ._templating import render


def table_indexes(cursor, schema: str, table: str) -> list[dict]:
    """[{"name", "unique", "shape"}, ...] for one table, where `shape` is the
    part of `pg_get_indexdef` after `USING` — e.g. `btree (company_id, created_at)`.

    The shape is what makes two indexes on two different tables comparable:
    names never match across a source table and a base table, and comparing
    Django's `Meta.indexes` against a vendor's DDL isn't possible at all."""
    cursor.execute(render("introspection/table_indexes.sql.j2"), [schema, table])
    return [{"name": name, "unique": unique, "shape": shape} for name, unique, shape in cursor.fetchall()]


def compare_indexes(source_indexes: list[dict], base_indexes: list[dict]) -> tuple[list[dict], list[dict]]:
    """(missing_locally, missing_at_source) — indexes present on one side with
    no same-shape counterpart on the other.

    Both directions matter. An index the source has and the base table doesn't
    makes the base half of the `UNION ALL` the slow half; one the base table
    has and the source doesn't makes the source half slow, and the source half
    is the one with 500M rows in it."""
    source_shapes = {index["shape"] for index in source_indexes}
    base_shapes = {index["shape"] for index in base_indexes}
    missing_locally = [index for index in source_indexes if index["shape"] not in base_shapes]
    missing_at_source = [index for index in base_indexes if index["shape"] not in source_shapes]
    return missing_locally, missing_at_source


# relkind for a declaratively-partitioned parent. Ordinary tables are "r".
PARTITIONED = "p"


def partition_summary(cursor, schema: str, table: str) -> dict | None:
    """`{"partitions": n, "unattached": [{"shape", "on_partitions"}, ...]}` for
    a partitioned parent, or None for an ordinary table.

    Two things parity cannot see without this.

    A partitioned parent is transparent to `table_indexes()` in the direction
    that flatters it: `CREATE INDEX` on the parent creates a partitioned index
    the catalogue reports under the parent, so the shape shows up and every
    partition really does have it. The direction it hides is the other one --
    an index built directly on one partition is attached to nothing, so the
    parent reports nothing and parity calls the shape missing everywhere when
    it exists on some. `unattached` is exactly that set, with how many
    partitions carry each shape, so half-covered is distinguishable from
    absent.

    The count is worth reporting on its own, because it is the multiplier on
    every probe that cannot prune. A lookup with no partition key in it is not
    one index scan, it is `partitions` of them.
    """
    cursor.execute(render("introspection/table_partitions.sql.j2"), [schema, table])
    row = cursor.fetchone()
    if row is None or row[0] != PARTITIONED:
        return None
    cursor.execute(render("introspection/unattached_partition_indexes.sql.j2"), [schema, table])
    return {
        "partitions": row[1],
        "unattached": [{"shape": shape, "on_partitions": count} for shape, count in cursor.fetchall()],
    }
