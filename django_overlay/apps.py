from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured


class DjangoOverlayConfig(AppConfig):
    name = "django_overlay"
    verbose_name = "Django Overlay"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        from . import checks  # also registers the manage.py check

        # Runs on every process boot, not just `manage.py check` — a bad
        # FK/M2M against an overlay model should fail loudly right away.
        errors = checks.check_no_plain_fk_to_overlay_models(None)
        if errors:
            raise ImproperlyConfigured(
                "django_overlay found unsafe references to overlay view models:\n"
                + "\n".join(f"{error.id}: {error.msg}" for error in errors)
            )
