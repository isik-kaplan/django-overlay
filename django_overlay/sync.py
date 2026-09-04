from django.apps import apps as django_apps
from django.db import connections, transaction

from . import sql as overlay_sql
from . import uniqueness
from ._templating import render
from .constraints import OverlayUniqueConstraint
from .fields import OverlayForeignKey, target_tables_for


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
        overlay_sql.build_instead_of_delete_sql(
            view_name, tenant_schema, base_table, pk_column, columns, soft_delete, source, strategy
        )
    )


def overlay_unique_constraints(model):
    """`model`'s OverlayUniqueConstraints, read off the base model where
    constraints live (see _BASE_ONLY_META_OPTIONS in models/meta.py)."""
    return [c for c in model._base_model._meta.constraints if isinstance(c, OverlayUniqueConstraint)]


def inbound_overlay_foreign_keys(target):
    """[(model, field), ...] for every concrete OverlayForeignKey in the
    project pointing at `target`, paired with the model whose *table* carries
    the trigger.

    Registry-wide, deliberately. makemigrations' equivalent
    (_fk_fields_targeting) stays inside one app because a reference from
    another app needs a migration in that app anyway, which Django generates on
    its own. A resync has no such excuse: it runs against live code with the
    whole registry loaded, and a cross-app foreign key left probing the old
    source is the same silent hole as a same-app one.

    View models are skipped because an overlay model declares every field
    twice. The trigger belongs on the base table, and the base model is what
    this returns -- the same model AddOverlayConstraint resolves against."""
    found = []
    for model in django_apps.get_models(include_auto_created=True):
        if getattr(model, "_is_overlay_view_model", False):
            continue
        for field in model._meta.get_fields():
            # `.concrete` directly rather than through getattr: the isinstance
            # short-circuits first, and an OverlayForeignKey is a Field, which
            # always has it. A default there guards against nothing and reads
            # as though it guards against something.
            if not isinstance(field, OverlayForeignKey) or not field.concrete:
                continue
            if field.remote_field.model is target:
                found.append((model, field))
    return found


def sync_source_triggers(model, tenant_schema: str, execute) -> None:
    """Rebuild every constraint trigger whose body names `model`'s source
    table, from live model state.

    Deliberately *not* part of sync_view(), which SyncOverlayView calls while
    replaying migrations. Those trigger bodies encode whether the target
    soft-deletes at that point in history, which is why AddOverlayConstraint
    goes to such lengths to read it out of historical state; rebuilding them
    from live code mid-replay would write a trigger referencing
    `_overlay_deleted` before the migration that adds it has run. There is no
    such ambiguity here -- a resync happens now, against the shape that exists
    now -- so this is the live path's job alone.

    Two families, and the split is by *which* source a body names:

    - Uniqueness triggers, on this model's base table, probing this model's
      source for a duplicate.
    - The insert side of every foreign key pointing *at* this model, on the
      referencing table, probing this model's source for the target row. The
      delete side goes with it: it reads the target through the view, so the
      source table itself never appears in its body, but the target source's
      partition_key does.

    A foreign key declared *on* this model is left alone on purpose. Its body
    names its own target's source, which this resync isn't changing."""
    base_model = model._base_model
    base_table = base_model._meta.db_table
    source = model.get_source()

    for constraint in overlay_unique_constraints(model):
        execute(
            overlay_sql.build_unique_constraint_trigger_sql(
                uniqueness.trigger_name(base_table, constraint.name),
                tenant_schema,
                base_table,
                [base_model._meta.get_field(name).column for name in constraint.fields],
                source,
                model._meta.pk.column,
                model._overlay_meta.strategy,
                model._overlay_meta.soft_delete,
            )
        )

    for referencing, field in inbound_overlay_foreign_keys(model):
        execute(
            overlay_sql.build_constraint_trigger_sql(
                field.trigger_name(referencing),
                tenant_schema,
                referencing._meta.db_table,
                field.column,
                target_tables_for(model, tenant_schema, partition_column=field.partition_column),
                referencing._meta.pk.column,
            )
        )
        execute(
            overlay_sql.build_referenced_row_trigger_sql(
                field.referenced_row_trigger_name(referencing),
                tenant_schema,
                referencing._meta.db_table,
                field.column,
                base_table,
                model._meta.db_table,
                model._meta.pk.column,
                source.partition_key,
            )
        )


def statement_executor(cursor):
    """A cursor's execute, called the way a migration's schema_editor calls it.

    Not a detail. `schema_editor.execute(sql)` passes `params=()`, so psycopg
    interpolates the string and a literal percent sign has to be doubled --
    which is why every trigger template that RAISEs writes `%%`. A bare
    `cursor.execute(sql)` passes no params at all, psycopg interpolates
    nothing, and that same template text installs a trigger whose RAISE has two
    placeholders and one argument. Postgres refuses it outright ("too many
    parameters specified for RAISE"), at the moment the trigger is created.

    So the live path passes the empty params the migration path passes, and one
    body of SQL keeps one escaping rule.
    """

    def execute(sql):
        cursor.execute(sql, ())

    return execute


def resync_view(model, using: str = "default", lock_timeout: str = "5s") -> None:
    """Regenerate one model's view, its INSTEAD OF triggers, and every
    constraint trigger that names its source — call this whenever a tenant's
    resolved get_source() changes without a field change, since makemigrations
    never sees that.

    One transaction, which is the whole point rather than tidiness. Postgres
    does DDL transactionally, so readers and writers see the old source or the
    new one and never a mixture. Run as separate autocommitted statements --
    which is what this did before, since a plain cursor is in autocommit -- the
    window between replacing the view and replacing the triggers is a window in
    which the view reads one table while the constraints that are supposed to
    guard it probe another.

    `lock_timeout` bounds the wait for the AccessExclusiveLock that
    CREATE OR REPLACE VIEW takes: better to fail and retry than to queue behind
    a long-running read and hold every subsequent query behind you."""
    connection = connections[using]
    tenant_schema = resolve_schema(connection)
    with transaction.atomic(using=using), connection.cursor() as cursor:
        # set_config() rather than SET LOCAL, which takes no bind parameters.
        # `true` scopes it to this transaction.
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", [lock_timeout])
        execute = statement_executor(cursor)
        sync_view(model, tenant_schema, execute)
        sync_source_triggers(model, tenant_schema, execute)
