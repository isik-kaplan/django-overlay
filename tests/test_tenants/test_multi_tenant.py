import pytest
from django.db import IntegrityError, transaction
from django_tenants.utils import schema_context

from django_overlay.exceptions import OverlayConfigurationError
from tests.testapp.models import Address, Person, PersonNote, Phone, SoftDeleteTest, SoftDeleteUniqueTest
from tests.testapp_shared.models import PersonSource, SoftDeleteTestSource, SoftDeleteUniqueTestSource


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


def test_soft_delete_masks_a_source_row_for_one_tenant_only(tenants):
    """The tombstone lives in the tenant's own base table, so masking is
    per-tenant even though the source row is shared."""
    source = SoftDeleteTestSource.objects.create(first_name="Shared Ada")
    view_id = -source.id

    with schema_context("org_a"):
        SoftDeleteTest.objects.filter(id=view_id).delete()
        assert not SoftDeleteTest.objects.filter(id=view_id).exists()

    with schema_context("org_b"):
        assert SoftDeleteTest.objects.get(id=view_id).first_name == "Shared Ada"


def test_a_freed_unique_value_is_freed_per_tenant(tenants):
    """The partial index is per-schema, so freeing a value in one tenant says
    nothing about another. Exercises the WHERE NOT _overlay_deleted predicate
    inside a real tenant schema."""
    with schema_context("org_a"):
        SoftDeleteUniqueTest.objects.create(ssn="tenant-ssn", email="a@x", first_name="A", last_name="A").delete()
        SoftDeleteUniqueTest.objects.create(ssn="tenant-ssn", email="a@x", first_name="A", last_name="A")
        assert SoftDeleteUniqueTest.objects.count() == 1

    with schema_context("org_b"):
        assert SoftDeleteUniqueTest.objects.count() == 0
        SoftDeleteUniqueTest.objects.create(ssn="tenant-ssn", email="a@x", first_name="A", last_name="A")


def test_the_source_side_unique_trigger_fires_inside_a_tenant_schema(tenants):
    source = SoftDeleteUniqueTestSource.objects.create(ssn="from-source", email="s@x", first_name="S", last_name="S")

    with schema_context("org_a"):
        with pytest.raises(IntegrityError, match="overlay unique violation"):
            with transaction.atomic():
                SoftDeleteUniqueTest.objects.create(ssn="from-source", email="o@x", first_name="O", last_name="O")

    with schema_context("org_b"):
        # Masking it in this tenant frees the value here, and only here.
        SoftDeleteUniqueTest.objects.filter(id=-source.id).delete()
        SoftDeleteUniqueTest.objects.create(ssn="from-source", email="o@x", first_name="O", last_name="O")


def test_the_fk_trigger_re_check_works_inside_a_tenant_schema(tenants):
    """KNOWN_ISSUES/01's guard reads the referencing table by name, which has
    to be schema-qualified or it resolves to the wrong tenant's table."""
    with schema_context("org_a"):
        person = Person.objects.create(first_name="Jane", age=1)
        note = PersonNote.objects.create(person=person, text="t")
        note.delete()
        person.delete()

    with schema_context("org_a"):
        assert not PersonNote.objects.exists()


def test_select_for_update_is_refused_inside_a_tenant_schema(tenants):
    with schema_context("org_a"), pytest.raises(OverlayConfigurationError):
        Person.objects.select_for_update()
