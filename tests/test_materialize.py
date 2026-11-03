"""`QuerySet.materialize()`: copy the matched rows into the base table.

For callers that change rows behind the ORM, typically raw SQL against the
base table, which only finds what the base table holds.
"""

import pytest
from django.db import connection

from tests.testapp.models import Person, SoftDeleteTest
from tests.testapp_shared.models import PersonSource, SoftDeleteTestSource


pytestmark = pytest.mark.django_db


def base_rows(model) -> dict:
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT id, first_name FROM "{model._base_model._meta.db_table}"')  # noqa: S608 - from _meta
        return dict(cursor.fetchall())


def test_a_source_row_is_copied_down_as_the_view_shows_it():
    source = PersonSource.objects.create(first_name="Src", age=7)

    Person.objects.filter(pk=-source.id).materialize()

    assert base_rows(Person) == {-source.id: "Src"}
    assert Person._base_model.objects.get(pk=-source.id).age == 7


def test_only_the_matched_rows_are_copied():
    kept, other = (PersonSource.objects.create(first_name=name, age=1) for name in ("Kept", "Other"))

    Person.objects.filter(first_name="Kept").materialize()

    assert set(base_rows(Person)) == {-kept.id}
    assert other.id


def test_a_row_already_in_the_base_table_is_left_as_it_is():
    source = PersonSource.objects.create(first_name="Src", age=1)
    person = Person.objects.get(pk=-source.id)
    person.first_name = "Edited"
    person.save()

    Person.objects.filter(pk=-source.id).materialize()

    assert base_rows(Person) == {-source.id: "Edited"}


def test_the_view_shows_each_row_once_afterwards():
    sources = [PersonSource.objects.create(first_name=f"S{n}", age=n) for n in range(3)]
    ours = Person.objects.create(first_name="Ours", age=9)

    Person.objects.all().materialize()

    assert sorted(Person.objects.values_list("pk", flat=True)) == sorted([ours.pk, *(-s.id for s in sources)])


def test_a_soft_delete_model_copies_rows_live():
    source = SoftDeleteTestSource.objects.create(first_name="Src")

    SoftDeleteTest.objects.filter(pk=-source.id).materialize()

    assert SoftDeleteTest._base_model.objects.get(pk=-source.id)._overlay_deleted is False
    assert SoftDeleteTest.objects.filter(pk=-source.id).exists()
