from django.db import migrations

from django_overlay.constraints import OverlayUniqueConstraint
from django_overlay.management.commands.makemigrations import (
    _overlay_base_to_view,
    _overlay_foreign_key_fields,
    extra_ops_for_migration,
)
from django_overlay.operations import AddOverlayConstraint, AddOverlayUniqueConstraint, SyncOverlayView


def test_base_to_view_maps_hidden_base_models_to_their_view_model():
    mapping = _overlay_base_to_view("testapp")
    assert mapping["personbase"] == "Person"
    assert mapping["metatestbase"] == "MetaTest"
    # The view model itself must never appear as a key — only its hidden
    # base table does, since that's the one operations actually touch.
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
    # Only the hidden base table's shape matters for the view's SQL — a
    # rename recorded against the view model's own migration state (which
    # mirrors the same field) would be a redundant, not a missed, trigger.
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
