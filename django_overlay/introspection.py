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
