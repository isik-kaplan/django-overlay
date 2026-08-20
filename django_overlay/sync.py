from django.db import connections

from . import sql as overlay_sql
from ._templating import render


def _columns_for(model) -> list:
    return [f.column for f in model._meta.fields]


def resolve_schema(connection) -> str:
    """django_tenants' schema_name if present, else current_schema()."""
    schema_name = getattr(connection, "schema_name", None)
    if schema_name:
        return schema_name
    with connection.cursor() as cursor:
        cursor.execute(render("introspection/current_schema.sql.j2"))
        return cursor.fetchone()[0]


def sync_view(model, tenant_schema: str, execute, columns=None, soft_delete=None) -> None:
    """Regenerates `model`'s view + its three INSTEAD OF triggers. Shared by
    SyncOverlayView and resync_view() — they just differ in what `execute`
    is (a schema_editor's or a plain cursor's).

    SyncOverlayView passes `columns` and `soft_delete` from migration-historical
    state rather than the live model: replaying migrations on a fresh database
    re-runs every past SyncOverlayView call, and each has to reflect the shape
    that existed at that point in history, not today's. `soft_delete` matters
    for the same reason `columns` does — turning it on adds `_overlay_deleted`,
    and a view rebuilt for an earlier migration must not filter on a column
    that migration hasn't added yet."""
    columns = columns if columns is not None else _columns_for(model)
    base_table = model._base_model._meta.db_table
    view_name = model._meta.db_table
    source = model.get_source()
    pk_column = model._meta.pk.column
    strategy = model._overlay_meta.strategy
    pk_default_sql = model._overlay_meta.pk_default_sql
    soft_delete = model._overlay_meta.soft_delete if soft_delete is None else soft_delete
    # Read live, not from historical state, because there is nothing historical
    # to read: soft_delete leaves a `_overlay_deleted` column behind that says
    # what it was at a point in history, and `overridable` leaves no trace in
    # the field list at all. Which is also why changing it generates no
    # migration — see OverlayMeta — and needs resync_overlay_views.
    overridable = model._overlay_meta.overridable

    execute(
        overlay_sql.build_view_sql(
            view_name, tenant_schema, base_table, source, columns, pk_column, strategy, soft_delete, overridable
        )
    )
    execute(
        overlay_sql.build_instead_of_insert_sql(
            view_name,
            tenant_schema,
            base_table,
            columns,
            pk_column,
            f"{base_table}_{pk_column}_seq",
            strategy,
            pk_default_sql,
            soft_delete,
            source,
            overridable,
        )
    )
    execute(
        overlay_sql.build_instead_of_update_sql(
            view_name, tenant_schema, base_table, columns, pk_column, soft_delete, overridable
        )
    )
    execute(
        overlay_sql.build_instead_of_delete_sql(view_name, tenant_schema, base_table, pk_column, columns, soft_delete)
    )


def resync_view(model, using: str = "default") -> None:
    """Regenerate one model's view + triggers outside of a migration — call
    this whenever a tenant's resolved get_source() changes without a field
    change, since makemigrations never sees that."""
    connection = connections[using]
    tenant_schema = resolve_schema(connection)
    with connection.cursor() as cursor:
        sync_view(model, tenant_schema, cursor.execute)
