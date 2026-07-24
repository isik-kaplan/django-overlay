import pytest
from django.db import IntegrityError, transaction

from django_overlay.fields import OverlayForeignKey, OverlayOneToOneField
from tests.testapp.models import Address, AddressNote, MetaTest, MetaTestNote, NullableFkTest, Person, PersonProfile


def test_overlay_foreign_key_never_creates_a_db_constraint():
    field = AddressNote._meta.get_field("address")
    assert field.db_constraint is False


def test_deconstruct_does_not_leak_db_constraint_as_an_explicit_kwarg():
    field = AddressNote._meta.get_field("address")
    _, _, _, kwargs = field.deconstruct()
    assert "db_constraint" not in kwargs


def test_trigger_name_is_stable_and_within_postgres_identifier_length():
    field = AddressNote._meta.get_field("address")
    name = field.trigger_name(AddressNote)
    assert name == "overlayfk_testapp_addressnote_address_id"
    assert len(name) <= 63


def test_target_tables_include_the_base_table_and_every_declared_source():
    field = AddressNote._meta.get_field("address")
    targets = field.target_tables("public")
    tables = [(t["schema"], t["table"]) for t in targets]
    assert ("public", Address.base_table()._meta.db_table) in tables
    assert ("public", "testapp_shared_addresssource") in tables


def test_target_tables_only_negate_the_source_side_for_a_negative_id_strategy_target():
    field = AddressNote._meta.get_field("address")
    targets = field.target_tables("public")
    negate_by_table = {t["table"]: t["negate"] for t in targets}
    assert negate_by_table[Address.base_table()._meta.db_table] is False
    assert negate_by_table["testapp_shared_addresssource"] is True


def test_overlay_one_to_one_field_is_also_an_overlay_foreign_key():
    field = PersonProfile._meta.get_field("person")
    assert isinstance(field, OverlayForeignKey)
    assert isinstance(field, OverlayOneToOneField)
    assert field.db_constraint is False
    assert field.one_to_one is True


def test_overlay_one_to_one_field_target_tables_point_at_person():
    targets = PersonProfile._meta.get_field("person").target_tables("public")
    tables = [(t["schema"], t["table"]) for t in targets]
    assert ("public", Person.base_table()._meta.db_table) in tables
    assert ("public", "testapp_shared_personsource") in tables


def test_target_tables_for_a_source_less_model_has_no_source_entry():
    targets = MetaTestNote._meta.get_field("meta_test").target_tables("public")
    assert len(targets) == 1
    assert targets[0]["table"] == MetaTest.base_table()._meta.db_table


@pytest.mark.django_db
def test_a_bonus_fk_to_a_source_less_model_round_trips_and_still_rejects_a_bogus_id(db_cursor):
    meta_test = MetaTest.objects.create(name="Has Note")

    note = MetaTestNote.objects.create(meta_test=meta_test, text="ok")
    assert list(meta_test.notes.all()) == [note]

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            MetaTestNote.objects.create(meta_test_id=-999999, text="should fail")
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.django_db
def test_a_nullable_overlay_foreign_key_allows_null():
    fk_test = NullableFkTest.objects.create(address=None)
    assert NullableFkTest.objects.get(pk=fk_test.pk).address_id is None


@pytest.mark.django_db
def test_a_nullable_overlay_foreign_key_still_rejects_a_non_null_bogus_id(db_cursor):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            NullableFkTest.objects.create(address_id=-999999)
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
