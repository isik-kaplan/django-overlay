from django.db import migrations

from django_overlay.constraints import OverlayUniqueConstraint
from django_overlay.management.commands.makemigrations import (
    _overlay_base_to_view,
    _overlay_foreign_key_fields,
    extra_ops_for_migration,
)
from django_overlay.operations import (
    AddOverlayConstraint,
    AddOverlayUniqueConstraint,
    RemoveOverlayConstraint,
    RemoveOverlayUniqueConstraint,
    SyncOverlayView,
)


def test_base_to_view_maps_hidden_base_models_to_their_view_model():
    mapping = _overlay_base_to_view("testapp")
    assert mapping["personbase"] == "Person"
    assert mapping["metatestbase"] == "MetaTest"
    assert "person" not in mapping


def test_foreign_key_fields_includes_bonus_table_fks():
    fields = _overlay_foreign_key_fields("testapp")
    assert ("addressnote", "address") in fields
    assert ("personprofile", "person") in fields


def test_rename_field_on_a_base_model_triggers_a_view_resync():
    base_to_view = _overlay_base_to_view("testapp")
    op = migrations.RenameField(model_name="MetaTestBase", old_name="name", new_name="full_name")

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    assert any(isinstance(o, SyncOverlayView) and o.model_name == "MetaTest" for o in extra_ops)


def test_rename_model_on_a_base_model_triggers_a_view_resync():
    base_to_view = _overlay_base_to_view("testapp")
    op = migrations.RenameModel(old_name="PersonBase", new_name="ClientBase")

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    assert any(isinstance(o, SyncOverlayView) and o.model_name == "Person" for o in extra_ops)


def test_renaming_a_view_models_own_field_does_not_trigger_a_resync():
    base_to_view = _overlay_base_to_view("testapp")
    op = migrations.RenameField(model_name="MetaTest", old_name="name", new_name="full_name")

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    assert not any(isinstance(o, SyncOverlayView) for o in extra_ops)


def test_renaming_an_overlay_foreign_key_column_retriggers_its_constraint():
    fk_fields = {("addressnote", "address")}
    op = migrations.RenameField(model_name="AddressNote", old_name="addr", new_name="address")

    extra_ops = extra_ops_for_migration("testapp", [op], {}, fk_fields)

    assert any(
        isinstance(o, AddOverlayConstraint) and o.model_name == "addressnote" and o.field_name == "address"
        for o in extra_ops
    )


def test_adding_an_overlay_unique_constraint_to_an_existing_model_is_detected():
    base_to_view = _overlay_base_to_view("testapp")
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="fake_constraint")
    op = migrations.AddConstraint(model_name="UniqueTestBase", constraint=constraint)

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    assert any(
        isinstance(o, AddOverlayUniqueConstraint)
        and o.model_name == "UniqueTest"
        and o.constraint_name == "fake_constraint"
        for o in extra_ops
    )


def test_a_plain_unique_constraint_is_not_detected():
    from django.db import models

    base_to_view = _overlay_base_to_view("testapp")
    constraint = models.UniqueConstraint(fields=["ssn"], name="plain_constraint")
    op = migrations.AddConstraint(model_name="UniqueTestBase", constraint=constraint)

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    assert not any(isinstance(o, AddOverlayUniqueConstraint) for o in extra_ops)


def test_removing_a_field_appends_a_remove_overlay_constraint():
    op = migrations.RemoveField(model_name="AddressNote", name="address")

    extra_ops = extra_ops_for_migration("testapp", [op], {}, set())

    matching = [o for o in extra_ops if isinstance(o, RemoveOverlayConstraint)]
    assert len(matching) == 1
    assert matching[0].model_name == "AddressNote"
    assert matching[0].field_name == "address"
    assert matching[0].column == "address_id"


def test_removing_a_field_appends_a_remove_overlay_constraint_regardless_of_field_type():
    op = migrations.RemoveField(model_name="MetaTest", name="some_plain_field")

    extra_ops = extra_ops_for_migration("testapp", [op], {}, set())

    assert any(isinstance(o, RemoveOverlayConstraint) for o in extra_ops)


def test_removing_a_constraint_on_an_overlay_model_appends_a_remove_overlay_unique_constraint():
    base_to_view = _overlay_base_to_view("testapp")
    op = migrations.RemoveConstraint(model_name="UniqueTestBase", name="uniquetest_ssn_unique")

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    matching = [o for o in extra_ops if isinstance(o, RemoveOverlayUniqueConstraint)]
    assert len(matching) == 1
    assert matching[0].model_name == "UniqueTest"
    assert matching[0].constraint_name == "uniquetest_ssn_unique"


def test_removing_a_constraint_on_a_non_overlay_model_is_ignored():
    op = migrations.RemoveConstraint(model_name="SomeUnrelatedModel", name="whatever")

    extra_ops = extra_ops_for_migration("testapp", [op], {}, set())

    assert not any(isinstance(o, RemoveOverlayUniqueConstraint) for o in extra_ops)
