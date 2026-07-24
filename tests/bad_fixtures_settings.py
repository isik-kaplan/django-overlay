"""Same as tests.django_settings, plus tests.bad_fixtures_app. Booting
under this settings module is expected to fail — see test_checks.py,
which uses it via a subprocess, not as a normal pytest invocation."""

import os


SECRET_KEY = "not-a-secret-just-for-tests"

DEBUG = False

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "django_overlay"),
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_overlay",
    "tests.testapp_shared",
    "tests.testapp",
    "tests.bad_fixtures_app",
]

MIDDLEWARE = []

USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
