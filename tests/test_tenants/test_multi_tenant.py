import pytest
from django_tenants.utils import schema_context

from tests.testapp.models import Address, Person, Phone
from tests.testapp_shared.models import PersonSource


pytestmark = pytest.mark.django_db


def test_untouched_source_row_is_visible_identically_from_every_tenant(tenants):
    source = PersonSource.objects.create(first_name="Shared Jane", age=50)
    view_id = -source.id

    with schema_context("org_a"):
        assert Person.objects.get(id=view_id).first_name == "Shared Jane"
    with schema_context("org_b"):
        assert Person.objects.get(id=view_id).first_name == "Shared Jane"


def test_materialize_in_one_tenant_does_not_affect_another_tenants_view(tenants):
    source = PersonSource.objects.create(first_name="Shared Bob", age=60)
    view_id = -source.id

    with schema_context("org_a"):
        Person.objects.filter(id=view_id).update(age=61)
        assert Person.objects.get(id=view_id).age == 61

    with schema_context("org_b"):
        assert Person.objects.get(id=view_id).age == 60


def test_many_to_many_works_inside_a_real_tenant_schema(tenants):
    with schema_context("org_a"):
        person = Person.objects.create(first_name="Tenant Person", age=25)
        address = Address.objects.create(street="1 Org A St", city="Org City")
        phone = Phone.objects.create(number="555-0001")

        person.addresses.add(address, through_defaults={"label": "office"})
        person.phones.add(phone, through_defaults={"label": "mobile"})

        assert list(person.addresses.all()) == [address]
        assert list(person.phones.all()) == [phone]
