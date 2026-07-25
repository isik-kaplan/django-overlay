"""Run as its own pytest invocation, separate from the main suite (see
docs/development/DEVELOPMENT.md) — proves _resolve_schema against a real per-tenant schema."""

import os


SECRET_KEY = "not-a-secret-just-for-tests"

DEBUG = False

DATABASES = {
    "default": {
        "ENGINE": "django_tenants.postgresql_backend",
        "NAME": os.environ.get("POSTGRES_TENANTS_DB", "django_overlay_tenants"),
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

DATABASE_ROUTERS = ["django_tenants.routers.TenantSyncRouter"]

# testapp_shared (source tables) lives once in public; testapp (overlay
# models) is per-tenant, each org getting its own base tables/views/triggers.
SHARED_APPS = [
    "django_tenants",
    "django.contrib.contenttypes",
    "django_overlay",
    "tests.tenants_app",
    "tests.testapp_shared",
]

TENANT_APPS = [
    "tests.testapp",
]

INSTALLED_APPS = list(SHARED_APPS) + [app for app in TENANT_APPS if app not in SHARED_APPS]

TENANT_MODEL = "tenants_app.Client"
TENANT_DOMAIN_MODEL = "tenants_app.Domain"

MIDDLEWARE = []

USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
