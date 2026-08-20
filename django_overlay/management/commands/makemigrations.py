from django.apps import apps as django_apps
from django.core.management.commands.makemigrations import Command as BaseCommand
from django.db import migrations

from ...constraints import OverlayUniqueConstraint
from ...fields import OverlayForeignKey
from ...models import OverlayModelBase
from ...operations import (
    AddOverlayConstraint,
    AddOverlayUniqueConstraint,
    DropOverlayView,
    RemoveOverlayConstraint,
    RemoveOverlayUniqueConstraint,
    SyncOverlayView,
)


# Operation types whose references_model/references_field are actually
# implemented (the Operation base class default just returns True always).
# IndexOperation subclasses (AddConstraint, RemoveConstraint) fall in that
# "always True" bucket, so they're handled separately below.
_FIELD_SHAPE_OPS = (
    migrations.AddField,
    migrations.RemoveField,
    migrations.AlterField,
    migrations.RenameField,
    migrations.CreateModel,
    migrations.RenameModel,
)
_FK_RETRIGGER_OPS = (migrations.RenameField, migrations.AlterField)


def _overlay_base_to_view(app_label):
    """{base_model_name (lowercase): view_model_name} for every OverlayModel
    in this app."""
    return {
        model._meta.model_name: model._view_model.__name__
        for model in django_apps.get_app_config(app_label).get_models()
        if isinstance(model, OverlayModelBase) and hasattr(model, "_view_model")
    }


def _overlay_foreign_key_fields(app_label):
    """{(model_name (lowercase), field_name)} for every OverlayForeignKey
    field declared anywhere in this app — the declaring model doesn't have
    to be an OverlayModel itself (e.g. a bonus FK/through table)."""
    fields = set()
    for model in django_apps.get_app_config(app_label).get_models(include_auto_created=True):
        for field in model._meta.get_fields():
            if isinstance(field, OverlayForeignKey) and getattr(field, "concrete", False):
                fields.add((model._meta.model_name, field.name))
    return fields


def _fk_fields_targeting(app_label, base_model_name):
    """[(model_name, field_name), ...] in this app whose OverlayForeignKey
    points at the overlay model whose base model is `base_model_name`.

    Both FK triggers encode whether the *target* soft-deletes — the insert side
    to stop a tombstoned row being a valid target, the delete side to know a
    soft delete removes a row from the view. Turning soft_delete on or off adds
    or drops `_overlay_deleted`, so every trigger pointing at that model has to
    be rebuilt or it keeps enforcing the old shape."""
    app_config = django_apps.get_app_config(app_label)
    targets = {
        model._view_model
        for model in app_config.get_models()
        if isinstance(model, OverlayModelBase) and hasattr(model, "_view_model")
        if model._meta.model_name == base_model_name.lower()
    }
    if not targets:
        return []
    referencing = []
    for model in app_config.get_models(include_auto_created=True):
        # Skip the view half of an overlay model: it carries a copy of every
        # field, but the trigger belongs to the base table and that is the
        # model name AddOverlayConstraint resolves against.
        if getattr(model, "_is_overlay_view_model", False):
            continue
        for field in model._meta.get_fields():
            if isinstance(field, OverlayForeignKey) and getattr(field, "concrete", False):
                if field.remote_field.model in targets:
                    referencing.append((model._meta.model_name, field.name))
    return referencing


def _new_overlay_unique_constraints(op):
    """[(model_name, constraint), ...] for OverlayUniqueConstraints this
    operation introduces — from a brand-new model's `options["constraints"]`
    (post-optimizer shape) or a standalone AddConstraint on an existing one."""
    if isinstance(op, migrations.CreateModel):
        return [(op.name, c) for c in op.options.get("constraints", []) if isinstance(c, OverlayUniqueConstraint)]
    if isinstance(op, migrations.AddConstraint) and isinstance(op.constraint, OverlayUniqueConstraint):
        return [(op.model_name, op.constraint)]
    return []


def _insert_view_drops_before_destructive_ops(app_label, operations):
    """Postgres refuses to drop a table or a column while a view depends on it,
    and an overlay model's view selects every column of its base table. So both
    `DeleteModel` and `RemoveField` need the view out of the way first; the
    `SyncOverlayView` appended by extra_ops_for_migration puts it back,
    rebuilt for the new shape.

    Emitted unconditionally, same reasoning as RemoveField/RemoveConstraint
    below: `DROP VIEW IF EXISTS` is a no-op for a plain model with no view."""
    new_operations = []
    for op in operations:
        if isinstance(op, migrations.DeleteModel):
            new_operations.append(DropOverlayView(app_label, op.name))
        elif isinstance(op, migrations.RemoveField):
            new_operations.append(DropOverlayView(app_label, op.model_name))
        new_operations.append(op)
    return new_operations


def extra_ops_for_migration(app_label, operations, base_to_view, fk_fields):
    """The extra operations one migration should get, given this app's
    OverlayModel registry (see _overlay_base_to_view/_overlay_foreign_key_fields).
    Split out of Command.write_migration_files so it's directly testable.

    RemoveField/RemoveConstraint are handled unconditionally, not keyed
    against the registry: the removed field/constraint is already gone from
    live code by the time this runs, so there's nothing to check its type
    against. DROP TRIGGER IF EXISTS makes appending them speculatively safe."""
    extra_ops = []
    synced_views = set()
    added_constraints = set()
    added_unique_constraints = set()

    for op in operations:
        if isinstance(op, _FIELD_SHAPE_OPS):
            for base_name, view_name in base_to_view.items():
                if view_name not in synced_views and op.references_model(base_name, app_label):
                    synced_views.add(view_name)
                    extra_ops.append(SyncOverlayView(app_label, view_name))

        if isinstance(op, migrations.RemoveField):
            extra_ops.append(RemoveOverlayConstraint(app_label, op.model_name, op.name))

        if isinstance(op, (migrations.AddField, migrations.RemoveField)) and op.name == "_overlay_deleted":
            # soft_delete just changed on this model; rebuild the FK triggers
            # of everything pointing at it. Only reaches this app — a reference
            # from another app needs a migration there, which Django would
            # generate anyway for its own state.
            for model_name, field_name in _fk_fields_targeting(app_label, op.model_name):
                key = (model_name, field_name)
                if key not in added_constraints:
                    added_constraints.add(key)
                    extra_ops.append(AddOverlayConstraint(app_label, model_name, field_name))

        if isinstance(op, migrations.RemoveConstraint):
            view_name = base_to_view.get(op.model_name.lower())
            if view_name is not None:
                extra_ops.append(RemoveOverlayUniqueConstraint(app_label, view_name, op.name))

        if isinstance(op, migrations.AddField) and isinstance(op.field, OverlayForeignKey):
            key = (op.model_name.lower(), op.name)
            if key not in added_constraints:
                added_constraints.add(key)
                extra_ops.append(AddOverlayConstraint(app_label, op.model_name, op.name))
        elif isinstance(op, migrations.CreateModel):
            for field_name, field in op.fields:
                if isinstance(field, OverlayForeignKey):
                    key = (op.name.lower(), field_name)
                    if key not in added_constraints:
                        added_constraints.add(key)
                        extra_ops.append(AddOverlayConstraint(app_label, op.name, field_name))
        elif isinstance(op, _FK_RETRIGGER_OPS):
            for model_name, field_name in fk_fields:
                key = (model_name, field_name)
                if key not in added_constraints and op.references_field(model_name, field_name, app_label):
                    added_constraints.add(key)
                    extra_ops.append(AddOverlayConstraint(app_label, model_name, field_name))

        for model_name, constraint in _new_overlay_unique_constraints(op):
            view_name = base_to_view.get(model_name.lower())
            if view_name is None:
                continue
            key = (model_name.lower(), constraint.name)
            if key not in added_unique_constraints:
                added_unique_constraints.add(key)
                extra_ops.append(AddOverlayUniqueConstraint(app_label, view_name, constraint.name))

    return extra_ops


class Command(BaseCommand):
    """Same as Django's makemigrations, except any operation touching an
    OverlayModel's base table gets a SyncOverlayView appended, any
    OverlayForeignKey/OverlayUniqueConstraint add/rename/remove gets its
    matching Add/RemoveOverlay(Unique)Constraint appended, and any
    DeleteModel gets a DropOverlayView inserted just before it."""

    def write_migration_files(self, changes, *args, **kwargs):
        for app_label, app_migrations in changes.items():
            base_to_view = _overlay_base_to_view(app_label)
            fk_fields = _overlay_foreign_key_fields(app_label)
            for migration in app_migrations:
                migration.operations = _insert_view_drops_before_destructive_ops(app_label, migration.operations)
                migration.operations.extend(
                    extra_ops_for_migration(app_label, migration.operations, base_to_view, fk_fields)
                )
        return super().write_migration_files(changes, *args, **kwargs)
