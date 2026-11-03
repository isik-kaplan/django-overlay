"""The delete-side FK trigger on a target whose source is partitioned.

It asks whether the row is still visible through the view, and narrows that
lookup with the partition key so it prunes. The key is only a hint, though:
the row is identified by its id. A base row may have no key at all, and an
update may change it, and neither takes the row out of the view.
"""

import pytest
from django.db import IntegrityError, connection, transaction

from django_overlay import sql as overlay_sql
from tests.testapp.models import Address, Person, PersonAddressThrough


pytestmark = pytest.mark.django_db


@pytest.fixture
def keyed_on_age():
    """The delete side of PersonAddressThrough.person, rendered as if Person's
    source were partitioned on `age`, which is nullable."""
    field = PersonAddressThrough._meta.get_field("person")
    with connection.cursor() as cursor:
        cursor.execute(
            overlay_sql.build_referenced_row_trigger_sql(
                field.referenced_row_trigger_name(PersonAddressThrough),
                "public",
                PersonAddressThrough._meta.db_table,
                field.column,
                Person._base_model._meta.db_table,
                Person._meta.db_table,
                Person._meta.pk.column,
                partition_key="age",
            ),
            # With parameters, as a migration runs it: the template doubles its %.
            (),
        )


def linked_person(age):
    person = Person.objects.create(first_name="Linked", age=age)
    PersonAddressThrough.objects.create(person=person, address=Address.objects.create(street="1 Road"))
    return person


def checked_now(write):
    """Run `write` with every deferred check firing at its statement. Switching
    first also fires the checks already queued, while the rows they need are
    still there."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        write()


def test_a_row_without_a_key_can_be_updated(keyed_on_age):
    person = linked_person(age=None)

    checked_now(lambda: Person.objects.filter(pk=person.pk).update(first_name="Renamed"))

    assert Person.objects.get(pk=person.pk).first_name == "Renamed"


def test_a_row_whose_key_changes_can_be_updated(keyed_on_age):
    person = linked_person(age=30)

    checked_now(lambda: Person.objects.filter(pk=person.pk).update(age=31))

    assert Person.objects.get(pk=person.pk).age == 31


def test_deleting_a_linked_row_is_still_refused(keyed_on_age):
    person = linked_person(age=None)

    with pytest.raises(IntegrityError, match="still references"):
        checked_now(lambda: Person._base_model.objects.filter(pk=person.pk).delete())
