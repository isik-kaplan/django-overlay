import pytest
from django.core.management import call_command
from django_tenants.utils import get_public_schema_name


@pytest.fixture(scope="session", autouse=True)
def tenants(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        call_command("migrate_schemas", schema_name=get_public_schema_name(), interactive=False, verbosity=0)

        from tests.tenants_app.models import Client, Domain

        result = {}
        for schema in ("org_a", "org_b"):
            client, _ = Client.objects.get_or_create(schema_name=schema, defaults={"name": schema})
            Domain.objects.get_or_create(domain=f"{schema}.test.com", tenant=client, defaults={"is_primary": True})
            result[schema] = client
        yield result
