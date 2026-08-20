from ._templating import render
from .strategies import Strategy, default_pk_sql, negates_source_ids


def build_view_sql(
    view_name: str,
    tenant_schema: str,
    base_table: str,
    source,
    columns,
    pk_column: str = "id",
    strategy: Strategy = Strategy.NEGATIVE_ID,
    soft_delete: bool = False,
) -> str:
    source_context = None
    if source is not None:
        source_context = {
            "schema": source.schema,
            "table": source.table,
            "id_column": source.id_column,
            "extra_where": source.extra_where,
            "negate": negates_source_ids(strategy),
        }
    return render(
        "view/view.sql.j2",
        tenant_schema=tenant_schema,
        view_name=view_name,
        base_table=base_table,
        columns=columns,
        pk_column=pk_column,
        source=source_context,
        soft_delete=soft_delete,
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
    soft_delete: bool = False,
) -> str:
    pk_default_expr = pk_default_sql or default_pk_sql(strategy)
    if pk_default_expr is None:
        pk_default_expr = render(
            "pk_defaults/sequence_nextval.sql.j2", tenant_schema=tenant_schema, pk_sequence=pk_sequence
        )
    return render(
        "triggers/instead_of_insert.sql.j2",
        tenant_schema=tenant_schema,
        view_name=view_name,
        base_table=base_table,
        function_name=f"{view_name}_instead_of_insert",
        pk_column=pk_column,
        pk_default_expr=pk_default_expr,
        columns=columns,
        soft_delete=soft_delete,
    )


def build_instead_of_update_sql(
    view_name: str, tenant_schema: str, base_table: str, columns, pk_column: str = "id", soft_delete: bool = False
) -> str:
    return render(
        "triggers/instead_of_update.sql.j2",
        tenant_schema=tenant_schema,
        view_name=view_name,
        base_table=base_table,
        function_name=f"{view_name}_instead_of_update",
        pk_column=pk_column,
        columns=columns,
        soft_delete=soft_delete,
    )


def build_instead_of_delete_sql(
    view_name: str,
    tenant_schema: str,
    base_table: str,
    pk_column: str = "id",
    columns=None,
    soft_delete: bool = False,
) -> str:
    template = "triggers/instead_of_delete_soft.sql.j2" if soft_delete else "triggers/instead_of_delete.sql.j2"
    return render(
        template,
        tenant_schema=tenant_schema,
        view_name=view_name,
        base_table=base_table,
        function_name=f"{view_name}_instead_of_delete",
        pk_column=pk_column,
        columns=columns,
    )


def build_constraint_trigger_sql(
    trigger_name: str,
    tenant_schema: str,
    referencing_table: str,
    column: str,
    target_tables,
    referencing_pk: str = "id",
) -> str:
    """`target_tables`: see fields.target_tables_for(). `referencing_pk` lets
    the deferred trigger re-find its own row at COMMIT — see the template."""
    return render(
        "triggers/constraint_trigger.sql.j2",
        tenant_schema=tenant_schema,
        referencing_table=referencing_table,
        referencing_pk=referencing_pk,
        column=column,
        target_tables=target_tables,
        function_name=f"check_{trigger_name}",
        trigger_name=trigger_name,
    )


def build_referenced_row_trigger_sql(
    trigger_name: str,
    tenant_schema: str,
    referencing_table: str,
    column: str,
    target_table: str,
    target_view: str,
    target_pk: str = "id",
) -> str:
    """The delete side of one OverlayForeignKey: refuse to remove a row that is
    still referenced. Lives on the *target's* base table."""
    return render(
        "triggers/referenced_row_trigger.sql.j2",
        tenant_schema=tenant_schema,
        referencing_table=referencing_table,
        column=column,
        target_table=target_table,
        target_view=target_view,
        target_pk=target_pk,
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
    soft_delete: bool = False,
) -> str | None:
    """None if there's no source to guard against — Postgres's own UNIQUE
    constraint on the base table already covers base-vs-base."""
    if source is None:
        return None
    return render(
        "triggers/unique_constraint_trigger.sql.j2",
        tenant_schema=tenant_schema,
        base_table=base_table,
        function_name=f"check_{trigger_name}",
        trigger_name=trigger_name,
        columns=columns,
        pk_column=pk_column,
        negate=negates_source_ids(strategy),
        soft_delete=soft_delete,
        source={"schema": source.schema, "table": source.table, "id_column": source.id_column},
    )
