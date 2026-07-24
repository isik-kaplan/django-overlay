from django.apps import apps as django_apps
from django.db import migrations

from . import sql as overlay_sql
from . import sync as overlay_sync
from ._templating import render
from .fields import target_tables_for


def _resolve_schema(schema_editor) -> str:
    return overlay_sync.resolve_schema(schema_editor.connection)


def _drop_view(schema_editor, tenant_schema: str, view_name: str) -> None:
    schema_editor.execute(render("ddl/drop_view.sql.j2", tenant_schema=tenant_schema, view_name=view_name))


def _drop_trigger(schema_editor, tenant_schema: str, trigger_name: str, table_name: str) -> None:
    schema_editor.execute(
        render("ddl/drop_trigger.sql.j2", tenant_schema=tenant_schema, trigger_name=trigger_name, table_name=table_name)
    )


class SyncOverlayView(migrations.RunPython):
    """(Re)creates the view + its three INSTEAD OF triggers. `model_name`
    must be the view model (e.g. "Person"), not the hidden base model.

    Call `sync.resync_view()` directly when a tenant's source changes
    without a field change — makemigrations never sees that."""

    def __init__(self, app_label: str, model_name: str):
        self.app_label = app_label
        self.model_name = model_name

        def forward(apps, schema_editor):
            model = django_apps.get_model(app_label, model_name)
            tenant_schema = _resolve_schema(schema_editor)
            # Columns from historical state (`apps`), not live code:
            # replaying migrations from scratch re-runs every past
            # SyncOverlayView call, and each has to reflect the columns
            # that existed at that point, not today's field list.
            historical_base = apps.get_model(app_label, model._base_model._meta.model_name)
            columns = [f.column for f in historical_base._meta.fields]
            overlay_sync.sync_view(model, tenant_schema, schema_editor.execute, columns=columns)

        def backward(apps, schema_editor):
            model = django_apps.get_model(app_label, model_name)
            tenant_schema = _resolve_schema(schema_editor)
            _drop_view(schema_editor, tenant_schema, model._meta.db_table)

        super().__init__(forward, backward, elidable=False)

    def deconstruct(self):
        return (
            self.__class__.__qualname__,
            [self.app_label, self.model_name],
            {},
        )


class AddOverlayConstraint(migrations.RunPython):
    """Creates the constraint trigger backing one OverlayForeignKey, since
    db_constraint=False means Django emits no FK DDL for it."""

    def __init__(self, app_label: str, model_name: str, field_name: str):
        self.app_label = app_label
        self.model_name = model_name
        self.field_name = field_name

        def historical_field(apps):
            # A later migration could have removed this field from live
            # code — historical state always has it.
            model = apps.get_model(app_label, model_name)
            return model._meta.get_field(field_name)

        def forward(apps, schema_editor):
            model = django_apps.get_model(app_label, model_name)
            field = historical_field(apps)
            tenant_schema = _resolve_schema(schema_editor)
            trigger_name = field.trigger_name(model)
            # Resolve the target by identity, not via the historical
            # field's remote_field.model: that's a state-reconstructed
            # class with no get_source()/_overlay_meta. The live model has both.
            target_meta = field.remote_field.model._meta
            live_target = django_apps.get_model(target_meta.app_label, target_meta.model_name)
            targets = target_tables_for(live_target, tenant_schema)
            schema_editor.execute(
                overlay_sql.build_constraint_trigger_sql(
                    trigger_name, tenant_schema, model._meta.db_table, field.column, targets
                )
            )

        def backward(apps, schema_editor):
            model = django_apps.get_model(app_label, model_name)
            field = historical_field(apps)
            tenant_schema = _resolve_schema(schema_editor)
            _drop_trigger(schema_editor, tenant_schema, field.trigger_name(model), model._meta.db_table)

        super().__init__(forward, backward, elidable=False)

    def deconstruct(self):
        return (
            self.__class__.__qualname__,
            [self.app_label, self.model_name, self.field_name],
            {},
        )


class AddOverlayUniqueConstraint(migrations.RunPython):
    """Creates the trigger backing one OverlayUniqueConstraint, guarding the
    base table against a value already present in the source table.
    `model_name` is the view model — the constraint lives on its base model's Meta."""

    def __init__(self, app_label: str, model_name: str, constraint_name: str):
        self.app_label = app_label
        self.model_name = model_name
        self.constraint_name = constraint_name

        def trigger_name(base_model) -> str:
            return f"overlayunique_{base_model._meta.db_table}_{constraint_name}"[:63]

        def historical_columns(apps, base_model):
            # Same reasoning as AddOverlayConstraint.historical_field.
            historical_base = apps.get_model(app_label, base_model._meta.model_name)
            constraint = next(c for c in historical_base._meta.constraints if c.name == constraint_name)
            return [historical_base._meta.get_field(f).column for f in constraint.fields]

        def forward(apps, schema_editor):
            model = django_apps.get_model(app_label, model_name)
            base_model = model._base_model
            columns = historical_columns(apps, base_model)
            tenant_schema = _resolve_schema(schema_editor)
            sql = overlay_sql.build_unique_constraint_trigger_sql(
                trigger_name(base_model),
                tenant_schema,
                base_model._meta.db_table,
                columns,
                model.get_source(),
                model._meta.pk.column,
                model._overlay_meta.strategy,
            )
            if sql:
                schema_editor.execute(sql)

        def backward(apps, schema_editor):
            model = django_apps.get_model(app_label, model_name)
            base_model = model._base_model
            tenant_schema = _resolve_schema(schema_editor)
            _drop_trigger(schema_editor, tenant_schema, trigger_name(base_model), base_model._meta.db_table)

        super().__init__(forward, backward, elidable=False)

    def deconstruct(self):
        return (
            self.__class__.__qualname__,
            [self.app_label, self.model_name, self.constraint_name],
            {},
        )


class RemoveOverlayConstraint(migrations.RunPython):
    """Drops the constraint trigger backing a removed OverlayForeignKey
    field. Dropping the column alone isn't enough — the trigger function's
    body still references it as literal PL/pgSQL text, so the *next*
    write, not the DROP COLUMN itself, would fail.

    `column` defaults to Django's `<field_name>_id` convention; pass the
    real db_column if the field customized it. Safe to run unconditionally
    (DROP TRIGGER IF EXISTS) even if the field was never actually an
    OverlayForeignKey."""

    def __init__(self, app_label: str, model_name: str, field_name: str, column: str | None = None):
        self.app_label = app_label
        self.model_name = model_name
        self.field_name = field_name
        self.column = column or f"{field_name}_id"

        def forward(apps, schema_editor):
            model = django_apps.get_model(app_label, model_name)
            tenant_schema = _resolve_schema(schema_editor)
            trigger_name = f"overlayfk_{model._meta.db_table}_{self.column}"[:63]
            _drop_trigger(schema_editor, tenant_schema, trigger_name, model._meta.db_table)

        def backward(apps, schema_editor):
            pass  # the removed field no longer exists to re-derive target_tables() from

        super().__init__(forward, backward, elidable=False)

    def deconstruct(self):
        kwargs = {} if self.column == f"{self.field_name}_id" else {"column": self.column}
        return (
            self.__class__.__qualname__,
            [self.app_label, self.model_name, self.field_name],
            kwargs,
        )


class RemoveOverlayUniqueConstraint(migrations.RunPython):
    """Drops the trigger backing a removed OverlayUniqueConstraint. Django
    already drops the native UNIQUE constraint; this drops the extra
    base-vs-source trigger. `model_name` is the view model, same as
    AddOverlayUniqueConstraint."""

    def __init__(self, app_label: str, model_name: str, constraint_name: str):
        self.app_label = app_label
        self.model_name = model_name
        self.constraint_name = constraint_name

        def trigger_name(base_model) -> str:
            return f"overlayunique_{base_model._meta.db_table}_{constraint_name}"[:63]

        def forward(apps, schema_editor):
            model = django_apps.get_model(app_label, model_name)
            base_model = model._base_model
            tenant_schema = _resolve_schema(schema_editor)
            _drop_trigger(schema_editor, tenant_schema, trigger_name(base_model), base_model._meta.db_table)

        def backward(apps, schema_editor):
            pass  # the removed constraint no longer exists to re-derive columns/source from

        super().__init__(forward, backward, elidable=False)

    def deconstruct(self):
        return (
            self.__class__.__qualname__,
            [self.app_label, self.model_name, self.constraint_name],
            {},
        )
