import pytest
from django.db import IntegrityError, models, transaction

from django_overlay.fields import OverlayForeignKey, OverlayOneToOneField
from django_overlay.models import OverlayConfigurationError
from tests.testapp.models import Address, AddressNote, MetaTest, MetaTestNote, NullableFkTest, Person, PersonProfile


def test_overlay_foreign_key_never_creates_a_db_constraint():
    field = AddressNote._meta.get_field("address")
    assert field.db_constraint is False


def test_overlay_foreign_key_rejects_an_explicit_db_constraint_kwarg():
    with pytest.raises(OverlayConfigurationError, match="db_constraint"):
        OverlayForeignKey(Address, on_delete=models.CASCADE, db_constraint=True)


def test_deconstruct_does_not_leak_db_constraint_as_an_explicit_kwarg():
    field = AddressNote._meta.get_field("address")
    _, _, _, kwargs = field.deconstruct()
    assert "db_constraint" not in kwargs


def test_trigger_name_is_stable_and_within_postgres_identifier_length():
    field = AddressNote._meta.get_field("address")
    name = field.trigger_name(AddressNote)
    assert name == "overlayfk_testapp_addressnote_address_id"
    assert len(name) <= 63


def test_passing_db_constraint_is_refused_with_the_reason():
    """The refusal explains why the field owns that argument, and five mutants
    lived in the explanation."""
    from django_overlay.exceptions import OverlayConfigurationError

    with pytest.raises(OverlayConfigurationError) as raised:
        OverlayForeignKey("testapp.Address", on_delete=models.CASCADE, db_constraint=True)

    assert str(raised.value) == (
        "OverlayForeignKey always sets db_constraint=False (Postgres can't hold a real FK "
        "against a view) — don't pass db_constraint yourself."
    )


def test_a_trigger_name_is_truncated_to_postgres_identifier_length():
    """`[:63]` is the identifier limit, and `<= 63` passes for `[:64]` too.

    Every fixture name is short, so the truncation never happened and the
    boundary was never tested -- while at 64 characters Postgres silently
    truncates for you, and two triggers that differ only past the limit become
    one.
    """

    class LongName:
        class _meta:
            db_table = "a" * 80

    field = AddressNote._meta.get_field("address")

    assert len(field.trigger_name(LongName)) == 63
    assert len(field.referenced_row_trigger_name(LongName)) == 63


def test_the_two_trigger_names_do_not_collide_after_truncation():
    """They share a prefix, so truncating to the same length must still leave
    them different."""

    class LongName:
        class _meta:
            db_table = "b" * 80

    field = AddressNote._meta.get_field("address")

    assert field.trigger_name(LongName) != field.referenced_row_trigger_name(LongName)


def test_a_foreign_key_takes_on_delete_positionally():
    """ForeignKey's second argument is positional, and it rides in *args.

    Every field in the fixtures passes on_delete by keyword, so dropping *args
    from the forwarding changed nothing any test could see -- while a field
    declared the ordinary Django way would raise TypeError at import.
    """
    field = OverlayForeignKey("testapp.Address", models.CASCADE)

    assert field.remote_field.on_delete is models.CASCADE


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


def test_the_soft_delete_override_is_used_only_when_it_is_given():
    """`soft_delete if soft_delete is None else ...` -- the condition, inverted.

    The override exists so a trigger rebuilt while replaying an older migration
    does not reference _overlay_deleted before the migration that adds it has
    run. Nothing passed it, so the branch that reads the model's own setting was
    the only one ever taken.
    """
    field = AddressNote._meta.get_field("address")
    from django_overlay.fields import target_tables_for

    default = target_tables_for(Address, "public")
    forced_off = target_tables_for(Address, "public", soft_delete=False)
    forced_on = target_tables_for(Address, "public", soft_delete=True)

    assert default[0]["soft_delete"] == Address._overlay_meta.soft_delete
    assert forced_off[0]["soft_delete"] is False
    assert forced_on[0]["soft_delete"] is True
    assert field.target_tables("public")[0]["soft_delete"] == default[0]["soft_delete"]


def test_the_source_entry_never_carries_soft_delete():
    """A vendor table has no tombstone column of its own, so this key is always
    False on the source entry -- hiding a masked source row is `masked_by`'s
    job. Both have to be spelled the way the template reads them."""
    targets = AddressNote._meta.get_field("address").target_tables("public")
    source = [t for t in targets if t["table"] == "testapp_shared_addresssource"][0]

    assert set(source) == {"schema", "table", "id_column", "negate", "soft_delete", "masked_by"}
    assert source["soft_delete"] is False


def test_the_source_entry_points_at_the_table_that_can_mask_it():
    """A tombstone hides the source row from the view, so the
    FK check has to go looking for one -- and it lives in the target's base
    table, under the un-negated id.

    Only when the target soft deletes. Otherwise no tombstone can exist and the
    extra EXISTS would be dead weight on every insert and every update of the
    column."""
    from django_overlay.fields import target_tables_for

    masking = target_tables_for(Address, "public", soft_delete=True)
    not_masking = target_tables_for(Address, "public", soft_delete=False)

    assert masking[1]["masked_by"] is masking[0], "the base entry itself, not a copy of it"
    assert masking[1]["masked_by"]["table"] == Address._base_model._meta.db_table
    assert not_masking[1]["masked_by"] is None


def test_deconstructing_a_field_that_never_had_db_constraint_is_fine():
    """`kwargs.pop("db_constraint", None)` -- the default is what stops a
    KeyError on a field where Django never put the key there."""
    field = OverlayForeignKey("testapp.Address", on_delete=models.CASCADE)
    field.set_attributes_from_name("address")

    _, _, _, kwargs = field.deconstruct()

    assert "db_constraint" not in kwargs


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
