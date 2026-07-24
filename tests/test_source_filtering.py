import pytest

from tests.testapp.models import FilteredSourceTest
from tests.testapp_shared.models import FilteredSourceTestSource


pytestmark = pytest.mark.django_db


def test_extra_where_hides_rows_that_do_not_match():
    FilteredSourceTestSource.objects.create(first_name="Included", active=True)
    excluded = FilteredSourceTestSource.objects.create(first_name="Excluded", active=False)

    assert list(FilteredSourceTest.objects.values_list("first_name", flat=True)) == ["Included"]
    assert not FilteredSourceTest.objects.filter(id=-excluded.id).exists()


def test_extra_where_still_allows_organic_rows_through():
    FilteredSourceTestSource.objects.create(first_name="Excluded", active=False)

    organic = FilteredSourceTest.objects.create(first_name="Organic")

    names = set(FilteredSourceTest.objects.values_list("first_name", flat=True))
    assert names == {"Organic"}
    assert FilteredSourceTest.objects.get(id=organic.id).first_name == "Organic"
