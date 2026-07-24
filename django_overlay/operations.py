from django.apps import apps as django_apps
from django.db import migrations

from . import sql as overlay_sql
from . import sync as overlay_sync


def _resolve_schema(schema_editor) -> str:
    return overlay_sync.resolve_schema(schema_editor.connection)


class SyncOverlayView(migrations.RunPython):
    """(Re)creates the view + its three INSTEAD OF triggers, generated
    fresh from the model's current field list; CREATE OR REPLACE makes this
    idempotent. `model_name` must be the view model (e.g. "Person"), not
    the hidden base model.

    Also call `sync.resync_view()` directly whenever a tenant's resolved
    source changes without a field change — makemigrations never sees
    that."""

    def __init__(self, app_label: str, model_name: str):
        self.app_label = app_label
        self.model_name = model_name

        def forward(apps, schema_editor):
            model = django_apps.get_model(app_label, model_name)
            tenant_schema = _resolve_schema(schema_editor)
            overlay_sync.sync_view(model, tenant_schema, schema_editor.execute)

        def backward(apps, schema_editor):
            model = django_apps.get_model(app_label, model_name)
            tenant_schema = _resolve_schema(schema_editor)
            view_name = model._meta.db_table
            schema_editor.execute(f'DROP VIEW IF EXISTS "{tenant_schema}"."{view_name}" CASCADE;')

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

        def forward(apps, schema_editor):
            model = django_apps.get_model(app_label, model_name)
            field = model._meta.get_field(field_name)
            tenant_schema = _resolve_schema(schema_editor)
            trigger_name = field.trigger_name(model)
            targets = [
                (t if "." in t else f'"{tenant_schema}"."{t}"', c, negate) for t, c, negate in field.target_tables()
            ]
            schema_editor.execute(
                overlay_sql.build_constraint_trigger_sql(
                    trigger_name, tenant_schema, model._meta.db_table, field.column, targets
                )
            )

        def backward(apps, schema_editor):
            model = django_apps.get_model(app_label, model_name)
            field = model._meta.get_field(field_name)
            tenant_schema = _resolve_schema(schema_editor)
            schema_editor.execute(
                f'DROP TRIGGER IF EXISTS {field.trigger_name(model)} ON "{tenant_schema}"."{model._meta.db_table}";'
            )

        super().__init__(forward, backward, elidable=False)

    def deconstruct(self):
        return (
            self.__class__.__qualname__,
            [self.app_label, self.model_name, self.field_name],
            {},
        )


class AddOverlayUniqueConstraint(migrations.RunPython):
    """Creates the constraint trigger backing one OverlayUniqueConstraint,
    guarding the base table against a value already present in the model's
    source table. `model_name` must be the view model (e.g. "Person") — the
    constraint itself lives on its hidden base model's Meta."""

    def __init__(self, app_label: str, model_name: str, constraint_name: str):
        self.app_label = app_label
        self.model_name = model_name
        self.constraint_name = constraint_name

        def trigger_name(base_model) -> str:
            return f"overlayunique_{base_model._meta.db_table}_{constraint_name}"[:63]

        def forward(apps, schema_editor):
            model = django_apps.get_model(app_label, model_name)
            base_model = model._base_model
            constraint = next(c for c in base_model._meta.constraints if c.name == constraint_name)
            columns = [base_model._meta.get_field(f).column for f in constraint.fields]
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
            schema_editor.execute(
                f'DROP TRIGGER IF EXISTS {trigger_name(base_model)} ON "{tenant_schema}"."{base_model._meta.db_table}";'
            )

        super().__init__(forward, backward, elidable=False)

    def deconstruct(self):
        return (
            self.__class__.__qualname__,
            [self.app_label, self.model_name, self.constraint_name],
            {},
        )
