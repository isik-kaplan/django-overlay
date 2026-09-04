from ._templating import render
from .strategies import Strategy, default_pk_sql, negates_source_ids


def _source_context(source, strategy: Strategy) -> dict | None:
    """The source table as the templates want it — including whether this
    strategy negates source ids, which decides how a base pk maps back to a
    source row."""
    if source is None:
        return None
    return {
        "schema": source.schema,
        "table": source.table,
        "id_column": source.id_column,
        "extra_where": source.extra_where,
        "negate": negates_source_ids(strategy),
        # None unless the source is declared partitioned. Every template that
        # probes the source by id reads it and adds the key to its predicate,
        # so the probe prunes to one partition instead of fanning out across
        # all of them. See SourceTable.partition_key.
        "partition_key": source.partition_key,
    }


def anti_join_kind(overridable: bool, soft_delete: bool) -> str | None:
    """Which anti-join the view needs to stop a source row appearing twice.

    "full"       — anything in the base table shadows its source row.
    "tombstones" — only a tombstone can, because nothing is ever materialised.
    None         — nothing can, so the source branch needs no check at all.

    Split out of the template so the three cases can be asserted directly.
    """
    if overridable:
        return "full"
    return "tombstones" if soft_delete else None


# The view's origin column, and the two literals it carries.
#
# One definition, because four places have to agree about them: the view
# template writes the literals, the queryset filters on them, the tests assert
# both, and a project reading `person.overlay_origin` compares against them.
#
# Underscore-prefixed like `_overlay_deleted`, so it stays out of the namespace
# a model's own fields live in.
ORIGIN_COLUMN = "_overlay_origin"
ORIGIN_BASE = "base"
ORIGIN_SOURCE = "source"


def build_view_sql(
    view_name: str,
    tenant_schema: str,
    base_table: str,
    source,
    columns,
    pk_column: str = "id",
    strategy: Strategy = Strategy.NEGATIVE_ID,
    soft_delete: bool = False,
    overridable: bool = True,
) -> str:
    return render(
        "view/view.sql.j2",
        tenant_schema=tenant_schema,
        view_name=view_name,
        base_table=base_table,
        columns=columns,
        pk_column=pk_column,
        source=_source_context(source, strategy),
        soft_delete=soft_delete,
        anti_join=anti_join_kind(overridable, soft_delete),
        origin_column=ORIGIN_COLUMN,
        origin_base=ORIGIN_BASE,
        origin_source=ORIGIN_SOURCE,
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
    source=None,
    overridable: bool = True,
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
        source=_source_context(source, strategy),
        overridable=overridable,
    )


def build_instead_of_update_sql(
    view_name: str,
    tenant_schema: str,
    base_table: str,
    columns,
    pk_column: str = "id",
    soft_delete: bool = False,
    overridable: bool = True,
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
        overridable=overridable,
    )


def build_instead_of_delete_sql(
    view_name: str,
    tenant_schema: str,
    base_table: str,
    pk_column: str = "id",
    columns=None,
    soft_delete: bool = False,
    source=None,
    strategy: Strategy = Strategy.NEGATIVE_ID,
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
        source=_source_context(source, strategy),
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
    partition_key: str | None = None,
) -> str:
    """The delete side of one OverlayForeignKey: refuse to remove a row that is
    still referenced. Lives on the *target's* base table.

    `partition_key` is the target source's, and needs no declaration on the
    referencing side: this trigger fires on the target's own base table, so
    OLD is a target row and already carries the key."""
    return render(
        "triggers/referenced_row_trigger.sql.j2",
        tenant_schema=tenant_schema,
        referencing_table=referencing_table,
        column=column,
        target_table=target_table,
        target_view=target_view,
        target_pk=target_pk,
        partition_key=partition_key,
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
) -> str:
    """The source-side half of an OverlayUniqueConstraint.

    Postgres's own UNIQUE on the base table already covers base-vs-base; this
    is what makes the constraint hold across the view. There is always a source
    to guard against, since a sourceless overlay model is refused outright.
    """
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
