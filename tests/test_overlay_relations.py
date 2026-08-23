"""Relation fields declared *on* an OverlayModel.

Both models the metaclass produces declare every concrete field, so a relation
has to have its base-model side hidden or the two clash — see
django_overlay.models._base_field_copy.
"""

import pytest
from django.db import IntegrityError, connection, models, transaction
from django.test.utils import isolate_apps

from django_overlay import checks
from django_overlay.sources import SourceTable
from django_overlay.fields import OverlayForeignKey
from django_overlay.models import OverlayMeta, OverlayModel
from django_overlay.strategies import Strategy
from tests.testapp.models import Person, PersonNote, Vendor, VendorThing
from tests.testapp_shared.models import PersonSource


pytestmark = pytest.mark.django_db


def test_only_the_view_model_claims_the_reverse_accessor():
    accessors = [rel.get_accessor_name() for rel in Vendor._meta.related_objects]

    assert accessors.count("things") == 1
    assert VendorThing._meta.get_field("vendor").remote_field.related_name == "things"
    assert VendorThing._base_model._meta.get_field("vendor").remote_field.related_name == "+"


def test_the_hidden_base_model_is_not_reachable_from_the_relation_target():
    # Otherwise Django's delete collector would follow it and delete base rows
    # directly, walking straight past the view's INSTEAD OF triggers.
    related_models = {rel.related_model for rel in Vendor._meta.related_objects}

    assert VendorThing in related_models
    assert VendorThing._base_model not in related_models


def test_plain_foreign_key_with_related_name_round_trips():
    vendor = Vendor.objects.create(name="Acme")

    thing = VendorThing.objects.create(vendor=vendor, label="widget")

    assert list(vendor.things.all()) == [thing]
    assert VendorThing.objects.select_related("vendor").get(pk=thing.pk).vendor.name == "Acme"


def test_overlay_foreign_key_between_two_overlay_models_round_trips():
    person = Person.objects.create(first_name="Jane", age=30)

    note = PersonNote.objects.create(person=person, text="hello")

    assert list(person.overlay_notes.all()) == [note]
    assert PersonNote.objects.select_related("person").get(pk=note.pk).person.first_name == "Jane"


def test_overlay_foreign_key_accepts_a_reference_to_an_untouched_source_row():
    source = PersonSource.objects.create(first_name="Source Jane", age=40)

    note = PersonNote.objects.create(person_id=-source.id, text="on a source row")

    assert PersonNote.objects.get(pk=note.pk).person.first_name == "Source Jane"


def test_overlay_foreign_key_between_two_overlay_models_rejects_a_bogus_id(db_cursor):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            PersonNote.objects.create(person_id=-999999, text="nope")
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_the_fk_trigger_lands_on_the_base_table_not_the_view(db_cursor):
    db_cursor.execute(
        "SELECT tgrelid::regclass::text FROM pg_trigger WHERE tgname = %s",
        [PersonNote._base_model._meta.get_field("person").trigger_name(PersonNote._base_model)],
    )

    assert db_cursor.fetchone()[0] == "personnote"


def test_cascade_from_the_relation_target_goes_through_the_view():
    vendor = Vendor.objects.create(name="Acme")
    thing = VendorThing.objects.create(vendor=vendor, label="widget")

    vendor.delete()

    assert not VendorThing.objects.filter(pk=thing.pk).exists()
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM vendorthing WHERE id = %s", [thing.pk])
        assert cursor.fetchone()[0] == 0


@pytest.mark.django_db(transaction=True)
def test_creating_and_deleting_a_reference_in_one_transaction_commits_cleanly():
    """The FK trigger is deferred to COMMIT and Postgres does not drop queued
    events for rows deleted later in the same transaction. Without the
    trigger's own existence re-check this raises a violation against a parent
    that is legitimately gone.

    PersonNote.person and not VendorThing.vendor: only an OverlayForeignKey
    gets a constraint trigger, so a plain FK exercises nothing here."""
    try:
        with transaction.atomic():
            person = Person.objects.create(first_name="Jane", age=30)
            note = PersonNote.objects.create(person=person, text="t")
            note.delete()
            person.delete()
    finally:
        PersonNote.objects.all().delete()
        Person.objects.all().delete()


@pytest.mark.django_db(transaction=True)
def test_cascading_a_delete_in_the_same_transaction_as_the_insert_commits_cleanly():
    try:
        with transaction.atomic():
            person = Person.objects.create(first_name="Jane", age=30)
            PersonNote.objects.create(person=person, text="t")
            person.delete()  # collector deletes the PersonNote first
    finally:
        PersonNote.objects.all().delete()
        Person.objects.all().delete()


@pytest.mark.django_db(transaction=True)
def test_a_reference_repointed_to_a_valid_row_in_the_same_transaction_commits_cleanly():
    """The insert queues an event for the bogus id; the update queues its own
    for the good one. Only the value that actually landed should be checked."""
    person = Person.objects.create(first_name="Jane", age=30)
    try:
        with transaction.atomic():
            note = PersonNote.objects.create(person=person, text="x")
            other = Person.objects.create(first_name="Bob", age=31)
            note.person = other
            note.save(update_fields=["person"])
    finally:
        PersonNote.objects.all().delete()
        Person.objects.all().delete()


@pytest.mark.django_db(transaction=True)
def test_a_dangling_reference_still_fails_at_commit():
    """The guard must not swallow real violations."""
    try:
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                PersonNote.objects.create(person_id=-999999, text="nope")
    finally:
        PersonNote.objects.all().delete()


@isolate_apps("tests.testapp")
def test_a_self_referential_overlay_foreign_key_builds_and_hides_only_the_base_side():
    """The shape most likely to trip the reverse-accessor hiding, because both
    ends of the relation are the same pair of models. Probed before; pinned
    here so a regression fails the suite rather than a report."""

    class SelfRef(OverlayModel):
        parent = OverlayForeignKey("self", null=True, on_delete=models.SET_NULL, related_name="children")

        class Meta:
            app_label = "testapp"

        class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
            table_name = "selfref_relations"

            @staticmethod
            def get_source():
                return SourceTable(schema="public", table="testapp_shared_personsource")

    assert SelfRef._meta.get_field("parent").remote_field.model is SelfRef
    assert SelfRef._meta.get_field("parent").remote_field.related_name == "children"
    assert SelfRef._base_model._meta.get_field("parent").remote_field.related_name == "+"
    assert [rel.get_accessor_name() for rel in SelfRef._meta.related_objects].count("children") == 1
    assert not checks.check_no_plain_fk_to_overlay_models(None)
