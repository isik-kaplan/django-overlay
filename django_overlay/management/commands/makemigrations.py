from django.apps import apps as django_apps
from django.core.management.commands.makemigrations import Command as BaseCommand
from django.db import migrations

from ...constraints import OverlayUniqueConstraint
from ...fields import OverlayForeignKey
from ...models import OverlayModelBase
from ...operations import (
    AddOverlayConstraint,
    AddOverlayUniqueConstraint,
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


def _new_overlay_unique_constraints(op):
    """[(model_name, constraint), ...] for OverlayUniqueConstraints this
    operation introduces — from a brand-new model's `options["constraints"]`
    (post-optimizer shape) or a standalone AddConstraint on an existing one."""
    if isinstance(op, migrations.CreateModel):
        return [(op.name, c) for c in op.options.get("constraints", []) if isinstance(c, OverlayUniqueConstraint)]
    if isinstance(op, migrations.AddConstraint) and isinstance(op.constraint, OverlayUniqueConstraint):
        return [(op.model_name, op.constraint)]
    return []


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
    OverlayModel's base table gets a SyncOverlayView appended, and any
    OverlayForeignKey/OverlayUniqueConstraint add/rename/remove gets its
    matching Add/RemoveOverlay(Unique)Constraint appended."""

    def write_migration_files(self, changes, *args, **kwargs):
        for app_label, app_migrations in changes.items():
            base_to_view = _overlay_base_to_view(app_label)
            fk_fields = _overlay_foreign_key_fields(app_label)
            for migration in app_migrations:
                migration.operations.extend(
                    extra_ops_for_migration(app_label, migration.operations, base_to_view, fk_fields)
                )
        return super().write_migration_files(changes, *args, **kwargs)
