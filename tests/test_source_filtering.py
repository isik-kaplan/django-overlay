import pytest
from django.db import IntegrityError, connection, transaction

from tests.testapp.models import FilteredSourceTest, FilteredSourceTestNote, FilteredUniqueTest
from tests.testapp_shared.models import FilteredSourceTestSource, FilteredUniqueTestSource


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


def checked_now(write):
    """Run `write` with every deferred constraint checked at the statement."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        write()


def test_a_link_to_a_source_row_the_view_shows_is_accepted():
    visible = FilteredSourceTestSource.objects.create(first_name="Included", active=True)

    checked_now(lambda: FilteredSourceTestNote.objects.create(target_id=-visible.id))

    assert FilteredSourceTestNote.objects.get().target.first_name == "Included"


def test_a_link_to_a_source_row_the_view_hides_is_refused():
    """The row exists in the source table, but not in the view: a link to it
    could be written and never read back."""
    hidden = FilteredSourceTestSource.objects.create(first_name="Excluded", active=False)

    with pytest.raises(IntegrityError, match="overlay FK violation"):
        checked_now(lambda: FilteredSourceTestNote.objects.create(target_id=-hidden.id))


def test_a_value_held_by_a_source_row_the_view_shows_is_taken():
    FilteredUniqueTestSource.objects.create(ssn="111", active=True)

    with pytest.raises(IntegrityError, match="overlay unique violation"):
        checked_now(lambda: FilteredUniqueTest.objects.create(ssn="111"))


def test_a_value_held_only_by_a_source_row_the_view_hides_is_free():
    FilteredUniqueTestSource.objects.create(ssn="222", active=False)

    checked_now(lambda: FilteredUniqueTest.objects.create(ssn="222"))

    assert list(FilteredUniqueTest.objects.values_list("ssn", flat=True)) == ["222"]
