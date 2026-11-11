from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured


class DjangoOverlayConfig(AppConfig):
    name = "django_overlay"
    verbose_name = "Django Overlay"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        from . import checks  # also registers the manage.py check

        _require_the_private_django_apis()
        _put_hidden_throughs_in_state()
        _list_hidden_through_tables()

        # Run on every process boot, not just `manage.py check`. That's the
        # hard stop: ready() is part of django.setup(), which every Django
        # process must complete before models are usable, and --skip-checks
        # doesn't reach it. A misconfigured model can't get as far as emitting
        # DDL or serving a request.
        errors = [
            *checks.check_no_plain_fk_to_overlay_models(None),
            *checks.check_overlay_uniqueness(None),
        ]
        if errors:
            raise ImproperlyConfigured(
                "django_overlay found misconfigured overlay models:\n\n"
                + "\n\n".join(
                    f"{error.id}: {error.msg}" + (f"\n\n{error.hint}" if error.hint else "") for error in errors
                )
            )


# Private Django APIs the migration operations call. A Django release that
# renames or drops one would otherwise surface as a migration that does the
# wrong thing, or fails halfway through a deploy; this makes it a clear error
# at boot instead. Each is used by operations.AlterOverlayField.
PRIVATE_SCHEMA_EDITOR_APIS = ("_field_should_be_altered", "_alter_column_comment_sql", "_constraint_names")


def _require_the_private_django_apis():
    import django
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor

    missing = [name for name in PRIVATE_SCHEMA_EDITOR_APIS if not hasattr(BaseDatabaseSchemaEditor, name)]
    if missing:
        raise ImproperlyConfigured(
            f"django_overlay relies on BaseDatabaseSchemaEditor.{', .'.join(missing)}, which Django "
            f"{django.get_version()} doesn't have. Pin Django to a version django_overlay supports "
            "(see docs/reference/COMPATIBILITY.md) until a release of django_overlay supports this one."
        )


def _put_hidden_throughs_in_state():
    """Make ProjectState.from_apps include the through models
    OverlayManyToManyField builds; see autodetector.py.

    It is what makemigrations and migrate both build the current models'
    state with, so the two keep agreeing whoever's commands they are: without
    this, makemigrations would write no table for them and migrate would
    report them deleted on every run. Wrapped once."""
    from django.db.migrations.state import ProjectState

    from .autodetector import with_hidden_throughs

    original = ProjectState.from_apps.__func__
    if getattr(original, "_adds_hidden_throughs", False):
        return

    def from_apps(cls, apps):
        return with_hidden_throughs(original(cls, apps))

    from_apps._adds_hidden_throughs = True
    ProjectState.from_apps = classmethod(from_apps)


def _list_hidden_through_tables():
    """Count the tables of hidden through models declared on overlay models
    among the project's tables, for `flush`.

    Django finds a many-to-many's table through the model declaring the field,
    and skips that model when it is unmanaged, as an overlay model's view is.
    So `flush`, and with it every TransactionTestCase, would leave those
    tables full, and fail to truncate the plain table at their other end, which
    they still reference. Wrapped once, whichever app's ready() runs first."""
    from django.db.backends.base.introspection import BaseDatabaseIntrospection

    original = BaseDatabaseIntrospection.django_table_names
    if getattr(original, "_lists_hidden_through_tables", False):
        return

    def django_table_names(self, only_existing=False, include_views=True):
        from django.db import router

        from .autodetector import hidden_throughs

        tables = set(original(self, only_existing=False, include_views=include_views))
        tables.update(
            through._meta.db_table
            for through in hidden_throughs()
            if not through._meta.auto_created._meta.managed
            and router.allow_migrate_model(self.connection.alias, through)
        )
        if only_existing:
            existing = set(self.table_names(include_views=include_views))
            tables = {table for table in tables if self.identifier_converter(table) in existing}
        return list(tables)

    django_table_names._lists_hidden_through_tables = True
    BaseDatabaseIntrospection.django_table_names = django_table_names
