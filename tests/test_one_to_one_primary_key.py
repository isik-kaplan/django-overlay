"""An overlay model keyed by a one-to-one link takes its primary key from the row it points at.

There is no id sequence behind such a key, so nothing the view's insert path
does may name one.
"""

import pytest
from django.db import connection

from tests.testapp.models import Person, PersonDetail
from tests.testapp_shared.models import PersonDetailSource, PersonSource


pytestmark = pytest.mark.django_db


def test_a_row_for_an_organic_person_is_created_through_the_view():
    person = Person.objects.create(first_name="Ours")

    PersonDetail.objects.create(person=person, bio="written here")

    assert PersonDetail.objects.get(pk=person.pk).bio == "written here"
    assert person.detail.bio == "written here"


def test_a_row_for_a_source_person_is_created_under_that_persons_id():
    source = PersonSource.objects.create(first_name="Theirs")
    person = Person.objects.get(pk=-source.id)

    PersonDetail.objects.create(person=person, bio="override")

    assert PersonDetail.objects.get(pk=-source.id).bio == "override"


def test_a_source_row_is_overridden_in_place():
    source = PersonSource.objects.create(first_name="Theirs")
    PersonDetailSource.objects.create(person_id=source.id, bio="from source")
    detail = PersonDetail.objects.get(pk=-source.id)

    detail.bio = "edited"
    detail.save()

    assert PersonDetail.objects.get(pk=-source.id).bio == "edited"
    assert PersonDetail._base_model.objects.filter(pk=-source.id).exists()


def test_no_sequence_is_named_for_it():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT prosrc FROM pg_proc WHERE proname = %s",
            [f"{PersonDetail._meta.db_table}_instead_of_insert"],
        )
        (body,) = cursor.fetchone()
    assert "nextval" not in body
