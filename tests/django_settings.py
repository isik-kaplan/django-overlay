"""Postgres-only: no SQLite fallback (views + INSTEAD OF triggers +
constraint triggers). Connection details come from the environment."""

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

# A second alias over the same test database, so that routing can be asserted
# at all. update() and _update() take `using=self.db` in three places, and with
# one alias configured `using(self.db)` and `using(None)` are the same call --
# every mutation of them survived. MIRROR means Django creates no second
# database and runs no second set of migrations; it is the same connection
# under another name, which is exactly enough to tell the two apart.
DATABASES["other"] = {**DATABASES["default"], "TEST": {"MIRROR": "default"}}

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_overlay",
    "tests.testapp_shared",
    "tests.testapp",
]

MIDDLEWARE = []

USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
