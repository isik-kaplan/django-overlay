from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured


class DjangoOverlayConfig(AppConfig):
    name = "django_overlay"
    verbose_name = "Django Overlay"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        from . import checks  # also registers the manage.py check

        _require_the_private_django_apis()

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
