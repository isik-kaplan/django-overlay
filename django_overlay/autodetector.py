"""Hidden through models in migration state.

Django keeps an auto-created through model out of migration state: its table
is created, altered and dropped along with the model declaring the field. That
breaks for an overlay model, whose fields live on its view, which is unmanaged
-- Django skips an unmanaged model's tables, and leaves its relations out of
migration state altogether. So a through model OverlayManyToManyField builds
goes into migration state as a model of its own (see
fields.build_hidden_through), and from there Django's own operations create,
alter and drop its table, on replay and in a squash as for any other model.

The live model stays auto_created, which is what the ORM, admin forms and DRF
read to treat the field as a plain many-to-many. Its state carries no
auto_created, so Django treats it as an ordinary model there.

The models reach migration state through ProjectState.from_apps, which both
makemigrations and migrate build their picture of the current models with;
apps.py wraps it. Not through an autodetector of the library's own: Django's
check commands.E001 wants makemigrations and migrate to share one, and a
project may already have another app's migrate (django-tenants ships one).
"""

from django.apps import apps as django_apps
from django.core.management.base import CommandError
from django.db import migrations
from django.db.migrations.state import ModelState


def hidden_throughs():
    """Every through model OverlayManyToManyField built, across all apps."""
    return [
        model
        for model in django_apps.get_models(include_auto_created=True)
        if getattr(model, "_overlay_hidden_through", False)
    ]


def with_hidden_throughs(state):
    """`state`, plus the state of each hidden through model whose declaring
    model it holds. A state that doesn't hold the declaring model is not the
    project's (a test's, say) and is left alone."""
    for through in hidden_throughs():
        declaring = through._meta.auto_created._meta
        if (declaring.app_label, declaring.model_name) not in state.models:
            continue
        if (through._meta.app_label, through._meta.model_name) in state.models:
            continue
        model_state = ModelState.from_model(through)
        model_state.options.pop("auto_created", None)
        state.add_model(model_state)
    return state


def refuse_dropping_a_through_table(from_state, changes):
    """Refuse migrations that drop a model's table to create a hidden through
    model in its place.

    That is what replacing an explicit through= with a built one looks like to
    Django: a model deleted and a different model created, with no field in
    common it could call a rename. Written as generated, it would drop the old
    table, rows and all, and create the new one empty. Recognised two ways: the
    field's through= named the deleted model before, or the two models use the
    same table. The first is the usual case, since a hand-written join model
    rarely has the table name Django gives a built one. makemigrations calls
    this before writing anything; a migration written by hand never reaches it."""
    deleted = {}
    created = []
    for app_label, app_migrations in changes.items():
        for migration in app_migrations:
            for op in migration.operations:
                if isinstance(op, migrations.DeleteModel):
                    old = from_state.models.get((app_label, op.name_lower))
                    if old is not None:
                        deleted[(app_label, op.name_lower)] = old
                elif isinstance(op, migrations.CreateModel) and _is_hidden_through(app_label, op.name):
                    created.append((app_label, op))
    by_table = {_table(key[0], old): old for key, old in deleted.items()}
    for app_label, op in created:
        table = _table(app_label, ModelState(app_label, op.name, op.fields, op.options))
        old = deleted.get(_previous_through(from_state, django_apps.get_model(app_label, op.name))) or by_table.get(
            table
        )
        if old is None:
            continue
        old_table = _table(old.app_label, old)
        replaced = (
            f"drops {old_table} and creates it again, empty"
            if old_table == table
            else (f"drops {old_table} and creates {table}, empty")
        )
        raise CommandError(
            f"{op.name} is the through model an OverlayManyToManyField built, in place of {old.name}. "
            f"The migration Django would write {replaced}, so every link in it is lost. Write that "
            "migration by hand: wrap the DeleteModel and the CreateModel in "
            "migrations.SeparateDatabaseAndState(state_operations=[...]), and turn the old table into "
            "the new one in database_operations -- its name, its columns' names and whatever else "
            "differs, such as its unique_together."
        )


def _previous_through(from_state, through):
    """(app_label, model_name) of the model the field that built `through`
    named as its through= in `from_state`, or None."""
    declaring = through._meta.auto_created
    field = next(f for f in declaring._meta.local_many_to_many if f.remote_field.through is through)
    old_model = from_state.models.get((declaring._meta.app_label, declaring._meta.model_name))
    old_field = old_model and old_model.fields.get(field.name)
    old_through = getattr(getattr(old_field, "remote_field", None), "through", None)
    if old_through is None:
        return None
    # Always a string in migration state: a field there is a deconstructed copy.
    app_label, _, name = old_through.rpartition(".")
    return (app_label or declaring._meta.app_label, name.lower())


def _table(app_label, model_state):
    return model_state.options.get("db_table") or f"{app_label}_{model_state.name_lower}"


def _is_hidden_through(app_label, name):
    try:
        return getattr(django_apps.get_model(app_label, name), "_overlay_hidden_through", False)
    except LookupError:
        return False
