import jinja2

from .strategies import Strategy, default_pk_sql, negates_source_ids


_ENV = jinja2.Environment(
    loader=jinja2.PackageLoader("django_overlay", "sql_templates"),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _render(template_name: str, **context) -> str:
    return _ENV.get_template(template_name).render(**context).strip()


def _qi(name: str) -> str:
    """Double-quote a Postgres identifier, so a reserved-word column/table
    name (e.g. `order`) doesn't break the generated SQL."""
    return '"' + name.replace('"', '""') + '"'


def build_view_sql(
    view_name: str,
    tenant_schema: str,
    base_table: str,
    source,
    columns,
    pk_column: str = "id",
    strategy: Strategy = Strategy.NEGATIVE_ID,
) -> str:
    pk_q = _qi(pk_column)

    def select_list(id_expr: str) -> str:
        parts = []
        for c in columns:
            if c == pk_column:
                parts.append(pk_q if id_expr == pk_q else f"{id_expr} AS {pk_q}")
            else:
                parts.append(_qi(c))
        return ", ".join(parts)

    branches = [f'SELECT {select_list(pk_q)} FROM "{tenant_schema}"."{base_table}"']
    if source is not None:
        negate = negates_source_ids(strategy)
        source_id_q = _qi(source.id_column)
        id_expr = f"-{source_id_q}" if negate else source_id_q
        not_yet_materialized = (
            f'{id_expr} NOT IN (SELECT {pk_q} FROM "{tenant_schema}"."{base_table}" WHERE {pk_q} IS NOT NULL)'
        )
        where = " AND ".join(p for p in (source.extra_where, not_yet_materialized) if p)
        branches.append(f"SELECT {select_list(id_expr)} FROM {source.qualified_name} WHERE {where}")
    return _render(
        "view.sql.j2",
        tenant_schema=tenant_schema,
        view_name=view_name,
        body="\nUNION ALL\n".join(branches),
    )


def build_instead_of_insert_sql(
    view_name: str,
    tenant_schema: str,
    base_table: str,
    columns,
    pk_column: str,
    pk_sequence: str,
    strategy: Strategy = Strategy.NEGATIVE_ID,
    pk_default_sql: str | None = None,
) -> str:
    pk_default_expr = pk_default_sql or default_pk_sql(strategy)
    if pk_default_expr is None:
        pk_default_expr = f'nextval(\'"{tenant_schema}"."{pk_sequence}"\')'
    return _render(
        "instead_of_insert.sql.j2",
        tenant_schema=tenant_schema,
        view_name=view_name,
        base_table=base_table,
        function_name=f"{view_name}_instead_of_insert",
        pk_column=_qi(pk_column),
        pk_default_expr=pk_default_expr,
        insert_columns=", ".join(_qi(c) for c in columns),
        insert_values=", ".join(f"NEW.{_qi(c)}" for c in columns),
    )


def build_instead_of_update_sql(
    view_name: str, tenant_schema: str, base_table: str, columns, pk_column: str = "id"
) -> str:
    # Only overwrite a column if this transaction's UPDATE actually changed
    # it (NEW differs from what it originally read as OLD); otherwise keep
    # whatever's currently stored, so a concurrent transaction that touched
    # a *different* column doesn't get its write silently clobbered by this
    # one's stale, pre-concurrent-write copy of an untouched column. The
    # base_table qualifier in the ELSE branch is required, not stylistic:
    # bare `col` is ambiguous once EXCLUDED is in scope (the ON CONFLICT
    # branch), since either relation could resolve it.
    base_table_q = _qi(base_table)

    def merge(c: str, proposed: str) -> str:
        q = _qi(c)
        return f"{q} = CASE WHEN NEW.{q} IS DISTINCT FROM OLD.{q} THEN {proposed} ELSE {base_table_q}.{q} END"

    return _render(
        "instead_of_update.sql.j2",
        tenant_schema=tenant_schema,
        view_name=view_name,
        base_table=base_table,
        function_name=f"{view_name}_instead_of_update",
        pk_column=_qi(pk_column),
        insert_columns=", ".join(_qi(c) for c in columns),
        insert_values=", ".join(f"NEW.{_qi(c)}" for c in columns),
        assignments=", ".join(merge(c, f"NEW.{_qi(c)}") for c in columns if c != pk_column),
        conflict_assignments=", ".join(merge(c, f"EXCLUDED.{_qi(c)}") for c in columns if c != pk_column),
    )


def build_instead_of_delete_sql(view_name: str, tenant_schema: str, base_table: str, pk_column: str = "id") -> str:
    return _render(
        "instead_of_delete.sql.j2",
        tenant_schema=tenant_schema,
        view_name=view_name,
        base_table=base_table,
        function_name=f"{view_name}_instead_of_delete",
        pk_column=_qi(pk_column),
    )


def build_constraint_trigger_sql(
    trigger_name: str, tenant_schema: str, referencing_table: str, column: str, target_tables
) -> str:
    column_q = _qi(column)
    # negate: a referencing row stores the id the view *presented*, so a
    # NEGATIVE_ID source check needs that negation undone to hit the raw id.
    checks = " OR ".join(
        f"EXISTS (SELECT 1 FROM {qualified} WHERE {_qi(id_col)} = {'-' if negate else ''}NEW.{column_q})"
        for qualified, id_col, negate in target_tables
    )
    return _render(
        "constraint_trigger.sql.j2",
        tenant_schema=tenant_schema,
        referencing_table=referencing_table,
        column=column,
        column_ident=column_q,
        checks=checks,
        function_name=f"check_{trigger_name}",
        trigger_name=trigger_name,
    )


def build_unique_constraint_trigger_sql(
    trigger_name: str,
    tenant_schema: str,
    base_table: str,
    columns,
    source,
    pk_column: str,
    strategy: Strategy = Strategy.NEGATIVE_ID,
) -> str | None:
    """None if there's no source to guard against — Postgres's own UNIQUE
    constraint on the base table already covers base-vs-base."""
    if source is None:
        return None
    pk_q = _qi(pk_column)
    # A materialized row's pk always equals its source row's raw id (mod
    # negation), so undoing that negation identifies "the source row this
    # base row came from" — excluded, or every materialized row would
    # immediately conflict with its own origin.
    own_source_id_expr = f"-NEW.{pk_q}" if negates_source_ids(strategy) else f"NEW.{pk_q}"
    not_null = " AND ".join(f"NEW.{_qi(c)} IS NOT NULL" for c in columns)
    value_match = " AND ".join(f"{_qi(c)} = NEW.{_qi(c)}" for c in columns)
    exists_check = (
        f"EXISTS (SELECT 1 FROM {source.qualified_name} "
        f"WHERE {value_match} AND {_qi(source.id_column)} != {own_source_id_expr})"
    )
    return _render(
        "unique_constraint_trigger.sql.j2",
        tenant_schema=tenant_schema,
        base_table=base_table,
        function_name=f"check_{trigger_name}",
        trigger_name=trigger_name,
        not_null=not_null,
        exists_check=exists_check,
    )
