import pytest

from tests.testapp.models import DigitLeadingTableNameTest, ReservedWord
from tests.testapp_shared.models import ReservedWordSource


pytestmark = pytest.mark.django_db


def test_full_round_trip_through_a_field_named_after_a_reserved_word():
    source = ReservedWordSource.objects.create(order=1)
    view_id = -source.id

    organic = ReservedWord.objects.create(order=5)
    assert ReservedWord.objects.get(id=organic.id).order == 5

    ReservedWord.objects.filter(id=view_id).update(order=99)
    assert ReservedWord.objects.get(id=view_id).order == 99

    organic.delete()
    assert not ReservedWord.objects.filter(id=organic.id).exists()


def test_full_round_trip_through_a_table_name_starting_with_a_digit():
    organic = DigitLeadingTableNameTest.objects.create(label="a")
    assert DigitLeadingTableNameTest.objects.get(id=organic.id).label == "a"

    DigitLeadingTableNameTest.objects.filter(id=organic.id).update(label="b")
    assert DigitLeadingTableNameTest.objects.get(id=organic.id).label == "b"

    organic.delete()
    assert not DigitLeadingTableNameTest.objects.filter(id=organic.id).exists()
