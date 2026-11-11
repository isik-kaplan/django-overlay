from django.apps import apps as django_apps
from django.core.management.commands.makemigrations import Command as BaseCommand
from django.db import migrations
from django.db.migrations.loader import MigrationLoader

from ...autodetector import refuse_dropping_a_through_table
from ...constraints import OverlayUniqueConstraint
from ...fields import OverlayForeignKey
from ...models import OverlayModelBase
from ...operations import (
    AddOverlayConstraint,
    AddOverlayUniqueConstraint,
    AlterOverlayField,
    DropOverlayView,
    RemoveOverlayConstraint,
    RemoveOverlayConstraintsOf,
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


def _unmanaged_models(app_label):
    """{model_name (lowercase)} of this app's unmanaged models, view halves included."""
    return {
        model._meta.model_name
        for model in django_apps.get_app_config(app_label).get_models(include_auto_created=True)
        if not model._meta.managed
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


def _delete_unreferenced_models_in_one_step(app_label, operations, from_state):
    """Drop the teardown Django puts before deleting a model nothing points at.

    Before a DeleteModel, Django's autodetector removes the model's
    unique_together/index_together and its relation fields one by one, so the
    deletions of several models that point at each other can be ordered. For a
    model no other model points at, the DROP TABLE does all of it. And one of
    those steps can't be squashed: a CreateModel's unique_together is created
    at the end of its migration, so once a squash puts the CreateModel and the
    AlterUniqueTogether removing it in one migration -- with the library's
    trigger operations between them, which keep the optimizer from folding the
    two -- the removal looks for a constraint that doesn't exist yet. Removing
    a many-to-many whose through model OverlayManyToManyField built is exactly
    this shape."""
    referrers = {}
    for key, model_state in from_state.models.items():
        for field in model_state.fields.values():
            if field.is_relation and not field.many_to_many:
                # Migration state names a relation's target as "app_label.model".
                referrers.setdefault(field.remote_field.model.lower(), set()).add(key)
    standalone = {
        op.name_lower
        for op in operations
        if isinstance(op, migrations.DeleteModel)
        and not referrers.get(f"{app_label}.{op.name_lower}", set()) - {(app_label, op.name_lower)}
    }
    teardown = (migrations.AlterUniqueTogether, migrations.AlterIndexTogether, migrations.RemoveField)
    return [op for op in operations if not (isinstance(op, teardown) and _model_of(op) in standalone)]


def _model_of(op):
    return op.model_name_lower if isinstance(op, migrations.RemoveField) else op.name_lower


def _insert_view_drops_before_destructive_ops(app_label, operations):
    """Postgres refuses to drop a table or a column while a view depends on it,
    and an overlay model's view selects every column of its base table. So both
    `DeleteModel` and `RemoveField` need the view out of the way first; the
    `SyncOverlayView` appended by extra_ops_for_migration puts it back,
    rebuilt for the new shape.

    The link triggers go first too, while historical state still has the model
    to name them from: RemoveOverlayConstraint before a RemoveField, and
    RemoveOverlayConstraintsOf before a DeleteModel.

    Emitted unconditionally, same reasoning as RemoveConstraint below: `DROP
    VIEW IF EXISTS` is a no-op for a plain model with no view, and `DROP
    TRIGGER IF EXISTS` for a field that was never an OverlayForeignKey."""
    new_operations = []
    for op in operations:
        if isinstance(op, migrations.DeleteModel):
            new_operations.append(DropOverlayView(app_label, op.name))
            new_operations.append(RemoveOverlayConstraintsOf(app_label, op.name))
        elif isinstance(op, migrations.RemoveField):
            new_operations.append(DropOverlayView(app_label, op.model_name))
            # Before, not after: a migration that deletes a model removes its
            # relations first, and once the DeleteModel has run there is no
            # model left to name the triggers from.
            new_operations.append(RemoveOverlayConstraint(app_label, op.model_name, op.name))
        new_operations.append(op)
    return new_operations


def _comment_aware_alter_fields(operations, base_to_view):
    """Each AlterField on an overlay model's base table, as an AlterOverlayField.

    A plain AlterField can't change a column's comment there: Django runs the
    change as an ALTER COLUMN TYPE, which Postgres refuses on a column the
    view selects. See AlterOverlayField."""
    return [
        AlterOverlayField(op.model_name, op.name, op.field, op.preserve_default)
        if type(op) is migrations.AlterField and op.model_name.lower() in base_to_view
        else op
        for op in operations
    ]


def extra_ops_for_migration(app_label, operations, base_to_view, fk_fields):
    """The extra operations one migration should get, given this app's
    OverlayModel registry (see _overlay_base_to_view/_overlay_foreign_key_fields).
    Split out of Command.write_migration_files so it's directly testable.

    RemoveConstraint is handled unconditionally, not keyed against the
    registry: the removed constraint is already gone from live code by the time
    this runs, so there's nothing to check its type against. DROP TRIGGER IF
    EXISTS makes appending it speculatively safe. RemoveField's
    RemoveOverlayConstraint is inserted before it instead; see
    _insert_view_drops_before_destructive_ops."""
    extra_ops = []
    synced_views = set()
    added_constraints = set()
    added_unique_constraints = set()
    # The view half of an overlay model carries a copy of every field, but an
    # OverlayForeignKey's trigger belongs on the base table. Django usually
    # leaves relations out of an unmanaged model's CreateModel, so the view
    # half rarely shows up here; a primary key cannot be left out, so a model
    # keyed by an OverlayOneToOneField does.
    view_models = {name.lower() for name in base_to_view.values()}
    # An unmanaged model's table is not Django's, so its links get no triggers;
    # see sync.inbound_overlay_foreign_keys.
    created_unmanaged = {
        op.name.lower()
        for op in operations
        if isinstance(op, migrations.CreateModel) and op.options.get("managed") is False
    }
    unmanaged = created_unmanaged | _unmanaged_models(app_label)

    for op in operations:
        if isinstance(op, _FIELD_SHAPE_OPS):
            for base_name, view_name in base_to_view.items():
                if view_name not in synced_views and op.references_model(base_name, app_label):
                    synced_views.add(view_name)
                    extra_ops.append(SyncOverlayView(app_label, view_name))

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
            if op.model_name.lower() not in view_models | unmanaged and key not in added_constraints:
                added_constraints.add(key)
                extra_ops.append(AddOverlayConstraint(app_label, op.model_name, op.name))
        elif isinstance(op, migrations.CreateModel) and op.name.lower() not in view_models | unmanaged:
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
    matching Add/RemoveOverlay(Unique)Constraint appended, any
    DeleteModel gets a DropOverlayView and a RemoveOverlayConstraintsOf inserted
    just before it, and an AlterField on a base table is written as an
    AlterOverlayField. It refuses a migration that would drop a table a built
    through model takes over; see autodetector.py."""

    def write_migration_files(self, changes, *args, **kwargs):
        # Before anything is rewritten: what Django generated is what would
        # drop a table, and the graph's current state is what it was diffed from.
        from_state = MigrationLoader(None, ignore_no_migrations=True).project_state()
        refuse_dropping_a_through_table(from_state, changes)
        for app_label, app_migrations in changes.items():
            base_to_view = _overlay_base_to_view(app_label)
            fk_fields = _overlay_foreign_key_fields(app_label)
            for migration in app_migrations:
                migration.operations = _delete_unreferenced_models_in_one_step(
                    app_label, migration.operations, from_state
                )
                migration.operations = _insert_view_drops_before_destructive_ops(app_label, migration.operations)
                migration.operations = _comment_aware_alter_fields(migration.operations, base_to_view)
                migration.operations.extend(
                    extra_ops_for_migration(app_label, migration.operations, base_to_view, fk_fields)
                )
        return super().write_migration_files(changes, *args, **kwargs)
