"""Referential integrity, from both sides.

The insert side refuses a reference to a row that isn't there. The delete side
refuses removing a row that is still referenced. Together they make integrity
enforced rather than advisory — including for writes that never go through
Django's delete collector, which is where the hole was: raw SQL, another
service on the same database, a data migration.

Everything here uses transaction=True, because both triggers are deferred to
COMMIT (as Django's own foreign keys are on PostgreSQL) and a rolled-back test
never reaches one.
"""

import pytest
from django.db import IntegrityError, connection, transaction

from tests.testapp.models import (
    Person,
    PersonNote,
    SoftDeleteTest,
    SoftDeleteTestNote,
)
from tests.testapp_shared.models import PersonSource, SoftDeleteTestSource


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _clean():
    yield
    PersonNote.objects.all().delete()
    Person.objects.all().delete()
    PersonSource.objects.all().delete()
    SoftDeleteTestNote.objects.all().delete()
    SoftDeleteTest.objects.all().delete()
    SoftDeleteTestSource.objects.all().delete()


def raw(sql, *params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)


# ------------------------------------------------------------- the hole


def test_raw_sql_cannot_delete_a_referenced_row():
    """The gap this trigger exists for. Django's collector isn't involved, so
    nothing else would have stopped it."""
    person = Person.objects.create(first_name="p", age=1)
    PersonNote.objects.create(person=person, text="t")

    with pytest.raises(IntegrityError, match="still references"):
        with transaction.atomic():
            raw("DELETE FROM person_view WHERE id = %s", person.pk)

    assert PersonNote.objects.count() == 1, "the reference is intact, not dangling"


def test_raw_sql_cannot_delete_the_base_row_directly_either():
    person = Person.objects.create(first_name="p", age=1)
    PersonNote.objects.create(person=person, text="t")

    with pytest.raises(IntegrityError, match="still references"):
        with transaction.atomic():
            raw("DELETE FROM person WHERE id = %s", person.pk)


def test_raw_sql_cannot_soft_delete_a_referenced_row():
    """Soft delete removes the row from the view without deleting it, so it
    needs the same guard."""
    target = SoftDeleteTest.objects.create(first_name="t")
    SoftDeleteTestNote.objects.create(target=target, text="n")

    with pytest.raises(IntegrityError, match="still references"):
        with transaction.atomic():
            raw("UPDATE softdeletetest SET _overlay_deleted = TRUE WHERE id = %s", target.pk)


# ------------------------------------------- what must still be allowed


def test_deleting_an_unreferenced_row_is_fine():
    person = Person.objects.create(first_name="p", age=1)

    with transaction.atomic():
        raw("DELETE FROM person_view WHERE id = %s", person.pk)

    assert not Person.objects.exists()


def test_the_orm_collector_still_cascades():
    person = Person.objects.create(first_name="p", age=1)
    PersonNote.objects.create(person=person, text="t")

    person.delete()

    assert not PersonNote.objects.exists()
    assert not Person.objects.exists()


def test_a_queryset_delete_still_cascades():
    person = Person.objects.create(first_name="p", age=1)
    PersonNote.objects.create(person=person, text="t")

    Person.objects.filter(pk=person.pk).delete()

    assert not PersonNote.objects.exists()


def test_reset_to_source_on_a_referenced_row_is_allowed():
    """The case the 'still visible through the view?' check exists for: the
    base copy goes, the source row shows through, the identity survives, so
    the reference is still valid and nothing should fire."""
    source = PersonSource.objects.create(first_name="Src", age=1)
    Person.objects.filter(pk=-source.id).update(age=2)  # materialise it
    PersonNote.objects.create(person_id=-source.id, text="t")

    with transaction.atomic():
        Person(pk=-source.id).reset_to_source()

    assert Person.objects.get(pk=-source.id).age == 1, "back to the pristine source value"
    assert PersonNote.objects.count() == 1


def test_deleting_a_materialised_row_whose_source_is_gone_is_refused():
    """Same shape as above but for a source-less model, where the identity
    really does disappear."""
    person = Person.objects.create(first_name="organic", age=1)
    PersonNote.objects.create(person=person, text="t")

    with pytest.raises(IntegrityError, match="still references"):
        with transaction.atomic():
            Person(pk=person.pk).reset_to_source()


def test_an_ordinary_update_of_a_referenced_row_is_untouched():
    target = SoftDeleteTest.objects.create(first_name="t")
    SoftDeleteTestNote.objects.create(target=target, text="n")

    with transaction.atomic():
        SoftDeleteTest.objects.filter(pk=target.pk).update(first_name="renamed")

    assert SoftDeleteTest.objects.get(pk=target.pk).first_name == "renamed"


def test_undoing_a_soft_delete_is_allowed():
    target = SoftDeleteTest.objects.create(first_name="t")

    with transaction.atomic():
        raw("UPDATE softdeletetest SET _overlay_deleted = TRUE WHERE id = %s", target.pk)
    with transaction.atomic():
        raw("UPDATE softdeletetest SET _overlay_deleted = FALSE WHERE id = %s", target.pk)

    assert SoftDeleteTest.objects.filter(pk=target.pk).exists()


# ------------------------------------------------------- the insert side


def test_a_reference_to_a_missing_row_is_still_refused():
    with pytest.raises(IntegrityError, match="not found in any target table"):
        with transaction.atomic():
            PersonNote.objects.create(person_id=-999999, text="x")


def test_raw_sql_cannot_insert_a_dangling_reference_either():
    with pytest.raises(IntegrityError, match="not found in any target table"):
        with transaction.atomic():
            raw("INSERT INTO personnote_view (person_id, text) VALUES (%s, %s)", -999999, "x")


def test_both_sides_together_survive_creating_and_removing_in_one_transaction():
    """The insert-side guard's own re-check has to keep working now that the
    delete side fires too."""
    with transaction.atomic():
        person = Person.objects.create(first_name="p", age=1)
        note = PersonNote.objects.create(person=person, text="t")
        note.delete()
        person.delete()

    assert not Person.objects.exists()


def test_set_null_is_unaffected_because_the_collector_runs_first():
    """CASCADE, PROTECT and SET_NULL all deal with the children before the
    parent row goes, so the delete-side guard never sees a live reference.

    on_delete=DO_NOTHING is the one that changes: it deliberately leaves the
    children, so the guard now refuses the delete. That is what Django
    documents for DO_NOTHING against a database that enforces integrity, and
    it is the same path as test_raw_sql_cannot_delete_a_referenced_row."""
    from tests.testapp.models import Address, NullableFkTest

    address = Address.objects.create(street="s", city="c")
    NullableFkTest.objects.create(address=address)  # on_delete=SET_NULL

    with transaction.atomic():
        address.delete()

    assert NullableFkTest.objects.get().address_id is None
    NullableFkTest.objects.all().delete()
    Address.objects.all().delete()
