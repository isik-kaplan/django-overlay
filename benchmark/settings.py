"""Django settings for a benchmark run.

The benchmark talks to a real database rather than a pytest test database, so
that the loaded graph and its `bench_cache` schema survive between runs. The
apps are the test project's: the bench models live in `tests/testapp` because
the permanent test suite depends on them too, and duplicating them here would
be two definitions of one schema.

Connection details come from `OVERLAY_BENCH_DATABASE_URL` when the CLI sets it,
and otherwise from the same POSTGRES_* variables the test suite uses.
"""

import os
from urllib.parse import unquote, urlparse

from benchmark import switches


SECRET_KEY = "not-a-secret-just-for-benchmarks"

DEBUG = False


def _from_url(url):
    parsed = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/") or "django_overlay",
        "USER": unquote(parsed.username or "postgres"),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "localhost",
        "PORT": str(parsed.port or 5432),
    }


_url = os.environ.get("OVERLAY_BENCH_DATABASE_URL")
DATABASES = {
    "default": _from_url(_url) if _url else {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "django_overlay_bench"),
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
]

MIDDLEWARE = []

USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"


# The five query optimisations, switchable from the environment.
#
# A benchmark that cannot turn an optimisation off cannot say what it is worth,
# and this module used to leave them all at their library defaults -- so the
# only measurable question was "overlay against a plain table", never "this
# rewrite against no rewrite". Comparing against master is not an option: none
# of these mechanisms exists there, and neither does this harness.
#
# The names come from benchmark/switches.py, which the CLI reads too, so a flag
# and the setting it moves cannot drift apart. Absent means on, matching the
# library's own `getattr(settings, name, True)`.
DJANGO_OVERLAY_REWRITE_TRAVERSALS = switches.read(switches.REWRITE_TRAVERSALS)
DJANGO_OVERLAY_REDIRECT_SELECT_RELATED = switches.read(switches.REDIRECT_SELECT_RELATED)
DJANGO_OVERLAY_FORCE_HASH_JOINS = switches.read(switches.FORCE_HASH_JOINS)
DJANGO_OVERLAY_ARRAY_SUBQUERY_IN = switches.read(switches.ARRAY_SUBQUERY_IN)
DJANGO_OVERLAY_M2M_FENCE = switches.read(switches.M2M_FENCE)
