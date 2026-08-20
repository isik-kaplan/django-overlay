from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured


class DjangoOverlayConfig(AppConfig):
    name = "django_overlay"
    verbose_name = "Django Overlay"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        from . import checks  # also registers the manage.py check

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
