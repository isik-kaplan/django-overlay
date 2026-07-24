from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured


class DjangoOverlayConfig(AppConfig):
    name = "django_overlay"
    verbose_name = "Django Overlay"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        from . import checks  # registers the system check (manage.py check) too

        # By the time any app's ready() runs, every app's models module has
        # already been imported (Django populates apps in two full passes —
        # models, then ready() — before either pass moves to the next app),
        # so this sees the complete model graph regardless of INSTALLED_APPS
        # order. Raising here means an unsafe FK/M2M against an overlay view
        # model fails the moment django.setup() runs — runserver, migrate, a
        # gunicorn/uwsgi worker, a Celery worker, manage.py shell, all of it —
        # not just when someone remembers to run manage.py check.
        errors = checks.check_no_plain_fk_to_overlay_models(None)
        if errors:
            raise ImproperlyConfigured(
                "django_overlay found unsafe references to overlay view models:\n"
                + "\n".join(f"{error.id}: {error.msg}" for error in errors)
            )
