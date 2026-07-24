import pytest
from django.db import IntegrityError, transaction

from django_overlay.fields import OverlayForeignKey, OverlayManyToManyField
from tests.testapp.registry import STRATEGIES


pytestmark = pytest.mark.django_db


def bogus_id(strategy_name):
    return -999999 if strategy_name == "negative_id" else "00000000-0000-0000-0000-000000000000"


def test_overlay_many_to_many_field_requires_an_explicit_through_model():
    m = STRATEGIES["negative_id"]
    with pytest.raises(TypeError, match="through"):
        OverlayManyToManyField(m["Address"])


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_through_model_uses_overlay_foreign_keys(strategy_name):
    m = STRATEGIES[strategy_name]
    through = m["PersonAddressThrough"]
    assert isinstance(through._meta.get_field("person"), OverlayForeignKey)
    assert isinstance(through._meta.get_field("address"), OverlayForeignKey)


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_add_and_query_round_trips_through_the_orm(strategy_name):
    m = STRATEGIES[strategy_name]
    person = m["Person"].objects.create(first_name="Alice", age=30)
    address = m["Address"].objects.create(street="1 Main St", city="Springfield")

    person.addresses.add(address, through_defaults={"label": "work"})

    assert list(person.addresses.all()) == [address]
    assert list(address.people.all()) == [person]
    link = m["PersonAddressThrough"].objects.get(person=person, address=address)
    assert link.label == "work"


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_through_model_rejects_a_reference_to_a_nonexistent_id(strategy_name, db_cursor):
    m = STRATEGIES[strategy_name]
    person = m["Person"].objects.create(first_name="Alice", age=30)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            m["PersonAddressThrough"].objects.create(person=person, address_id=bogus_id(strategy_name))
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_phonetag_bonus_many_to_many_round_trips_through_the_orm(strategy_name):
    m = STRATEGIES[strategy_name]
    phone = m["Phone"].objects.create(number="555-9999")
    tag = m["PhoneTag"].objects.create(name="mobile")

    tag.phones.add(phone)

    assert list(tag.phones.all()) == [phone]
    assert list(phone.tags.all()) == [tag]
