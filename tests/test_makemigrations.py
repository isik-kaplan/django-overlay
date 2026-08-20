from unittest.mock import patch

from django.db import migrations, models
from hypothesis import given
from hypothesis import strategies as st

from django_overlay.constraints import OverlayUniqueConstraint
from django_overlay.fields import OverlayForeignKey
from django_overlay.management.commands.makemigrations import (
    Command,
    _fk_fields_targeting,
    _insert_view_drops_before_destructive_ops,
    _overlay_base_to_view,
    _overlay_foreign_key_fields,
    extra_ops_for_migration,
)
from django_overlay.operations import (
    AddOverlayConstraint,
    AddOverlayUniqueConstraint,
    DropOverlayView,
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


def test_adding_a_field_to_an_existing_model_triggers_a_constraint():
    op = migrations.AddField(
        model_name="AddressNote",
        name="address",
        field=OverlayForeignKey("Address", on_delete=models.CASCADE),
    )

    extra_ops = extra_ops_for_migration("testapp", [op], {}, set())

    assert any(
        isinstance(o, AddOverlayConstraint) and o.model_name == "AddressNote" and o.field_name == "address"
        for o in extra_ops
    )


def test_creating_a_model_with_an_overlay_foreign_key_field_triggers_a_constraint():
    op = migrations.CreateModel(
        name="AddressNote",
        fields=[
            ("id", models.AutoField(primary_key=True)),
            ("address", OverlayForeignKey("Address", on_delete=models.CASCADE)),
        ],
    )

    extra_ops = extra_ops_for_migration("testapp", [op], {}, set())

    assert any(
        isinstance(o, AddOverlayConstraint) and o.model_name == "AddressNote" and o.field_name == "address"
        for o in extra_ops
    )


def test_adding_an_overlay_unique_constraint_to_a_brand_new_model_is_detected():
    base_to_view = _overlay_base_to_view("testapp")
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="fake_constraint")
    op = migrations.CreateModel(
        name="UniqueTestBase",
        fields=[("ssn", models.CharField(max_length=20))],
        options={"constraints": [constraint]},
    )

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    assert any(
        isinstance(o, AddOverlayUniqueConstraint)
        and o.model_name == "UniqueTest"
        and o.constraint_name == "fake_constraint"
        for o in extra_ops
    )


def test_adding_a_unique_constraint_on_a_non_overlay_model_is_ignored():
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="fake_constraint")
    op = migrations.AddConstraint(model_name="SomeUnrelatedModel", constraint=constraint)

    extra_ops = extra_ops_for_migration("testapp", [op], {}, set())

    assert not any(isinstance(o, AddOverlayUniqueConstraint) for o in extra_ops)


# The next three are contrived (a real migration wouldn't duplicate an
# operation like this), but they're valid input to the pure function and
# exercise the dedup guards directly: whichever operation notices a given
# key first wins, later ones for the same key are no-ops.
def test_the_same_fk_field_added_twice_only_adds_one_constraint():
    ops = [
        migrations.AddField(
            model_name="AddressNote", name="address", field=OverlayForeignKey("Address", on_delete=models.CASCADE)
        ),
        migrations.AddField(
            model_name="AddressNote", name="address", field=OverlayForeignKey("Address", on_delete=models.CASCADE)
        ),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, {}, set())

    assert len([o for o in extra_ops if isinstance(o, AddOverlayConstraint)]) == 1


def test_an_fk_field_added_and_then_seen_again_via_create_model_only_adds_one_constraint():
    ops = [
        migrations.AddField(model_name="Foo", name="bar", field=OverlayForeignKey("Address", on_delete=models.CASCADE)),
        migrations.CreateModel(name="Foo", fields=[("bar", OverlayForeignKey("Address", on_delete=models.CASCADE))]),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, {}, set())

    assert len([o for o in extra_ops if isinstance(o, AddOverlayConstraint)]) == 1


def test_the_same_new_unique_constraint_appearing_twice_only_adds_it_once():
    base_to_view = _overlay_base_to_view("testapp")
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="fake_constraint")
    ops = [
        migrations.AddConstraint(model_name="UniqueTestBase", constraint=constraint),
        migrations.AddConstraint(model_name="UniqueTestBase", constraint=constraint),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, base_to_view, set())

    assert len([o for o in extra_ops if isinstance(o, AddOverlayUniqueConstraint)]) == 1


def test_command_write_migration_files_appends_extra_ops_before_delegating():
    op = migrations.RenameField(model_name="MetaTestBase", old_name="name", new_name="full_name")
    migration = migrations.Migration("some_migration", "testapp")
    migration.operations = [op]
    changes = {"testapp": [migration]}

    with patch("django.core.management.commands.makemigrations.Command.write_migration_files") as base_write:
        Command().write_migration_files(changes)

    assert any(isinstance(o, SyncOverlayView) and o.model_name == "MetaTest" for o in migration.operations)
    base_write.assert_called_once_with(changes)


def test_deleting_a_model_gets_a_view_drop_inserted_immediately_before_it():
    op = migrations.DeleteModel(name="SomeModel")

    operations = _insert_view_drops_before_destructive_ops("testapp", [op])

    assert len(operations) == 2
    assert isinstance(operations[0], DropOverlayView)
    assert operations[0].model_name == "SomeModel"
    assert operations[1] is op


def test_deleting_two_models_gets_a_view_drop_inserted_before_each():
    op1 = migrations.DeleteModel(name="First")
    op2 = migrations.DeleteModel(name="Second")

    operations = _insert_view_drops_before_destructive_ops("testapp", [op1, op2])

    assert [type(o).__name__ for o in operations] == [
        "DropOverlayView",
        "DeleteModel",
        "DropOverlayView",
        "DeleteModel",
    ]
    assert operations[0].model_name == "First"
    assert operations[2].model_name == "Second"


def test_operations_without_a_delete_model_are_left_untouched():
    op = migrations.AddField(model_name="Foo", name="bar", field=models.CharField(max_length=1))

    operations = _insert_view_drops_before_destructive_ops("testapp", [op])

    assert operations == [op]


def test_command_write_migration_files_inserts_a_view_drop_before_delete_model():
    op = migrations.DeleteModel(name="MetaTestBase")
    migration = migrations.Migration("some_migration", "testapp")
    migration.operations = [op]
    changes = {"testapp": [migration]}

    with patch("django.core.management.commands.makemigrations.Command.write_migration_files") as base_write:
        Command().write_migration_files(changes)

    assert isinstance(migration.operations[0], DropOverlayView)
    assert migration.operations[0].model_name == "MetaTestBase"
    assert migration.operations[1] is op
    base_write.assert_called_once_with(changes)


_model_name = st.from_regex(r"[A-Za-z][A-Za-z0-9]{0,9}", fullmatch=True)


@given(op_specs=st.lists(st.tuples(st.sampled_from(["delete", "other"]), _model_name), max_size=15))
def test_every_delete_model_gets_a_view_drop_immediately_before_it_for_any_op_sequence(op_specs):
    operations = [
        migrations.DeleteModel(name=name)
        if kind == "delete"
        else migrations.AddField(model_name=name, name="f", field=models.CharField(max_length=1))
        for kind, name in op_specs
    ]

    result = _insert_view_drops_before_destructive_ops("testapp", operations)

    for i, op in enumerate(result):
        if isinstance(op, migrations.DeleteModel):
            assert i > 0
            assert isinstance(result[i - 1], DropOverlayView)
            assert result[i - 1].model_name == op.name

    assert [op for op in result if not isinstance(op, DropOverlayView)] == operations


@given(n=st.integers(min_value=1, max_value=20))
def test_the_same_fk_field_added_any_number_of_times_only_adds_one_constraint(n):
    ops = [
        migrations.AddField(
            model_name="AddressNote", name="address", field=OverlayForeignKey("Address", on_delete=models.CASCADE)
        )
        for _ in range(n)
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, {}, set())

    assert len([o for o in extra_ops if isinstance(o, AddOverlayConstraint)]) == 1


@given(n=st.integers(min_value=1, max_value=20))
def test_the_same_new_unique_constraint_appearing_any_number_of_times_only_adds_it_once(n):
    base_to_view = _overlay_base_to_view("testapp")
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="fake_constraint")
    ops = [migrations.AddConstraint(model_name="UniqueTestBase", constraint=constraint) for _ in range(n)]

    extra_ops = extra_ops_for_migration("testapp", ops, base_to_view, set())

    assert len([o for o in extra_ops if isinstance(o, AddOverlayUniqueConstraint)]) == 1


def test_a_remove_field_also_gets_a_view_drop_first():
    """Postgres refuses to drop a column a view selects, and the overlay view
    selects every column of its base table."""
    op = migrations.RemoveField(model_name="PersonBase", name="age")

    operations = _insert_view_drops_before_destructive_ops("testapp", [op])

    assert isinstance(operations[0], DropOverlayView)
    assert operations[0].model_name == "PersonBase"
    assert operations[1] is op


def test_operations_that_do_not_destroy_anything_get_no_view_drop():
    ops = [
        migrations.AddField(model_name="PersonBase", name="x", field=models.CharField(max_length=1)),
        migrations.AlterField(model_name="PersonBase", name="x", field=models.CharField(max_length=2)),
    ]

    assert _insert_view_drops_before_destructive_ops("testapp", ops) == ops


# Turning soft_delete on or off adds or drops _overlay_deleted, and both FK
# triggers encode whether the target soft-deletes — the insert side so a
# tombstoned row stops being a valid target, the delete side because a soft
# delete removes a row from the view with an UPDATE rather than a DELETE. So
# every trigger pointing at that model has to be rebuilt, or it goes on
# enforcing the shape the model had when the FK was created.


def test_fk_fields_targeting_finds_what_points_at_a_model():
    referencing = _fk_fields_targeting("testapp", "PersonBase")

    assert ("personnotebase", "person") in referencing, "an overlay model pointing at it"
    assert ("personprofile", "person") in referencing, "a plain model pointing at it"
    assert ("renamefktest", "renamed_fk") in referencing


def test_fk_fields_targeting_names_the_base_model_not_the_view():
    """AddOverlayConstraint resolves the model it is given, and the trigger
    belongs to the base table."""
    referencing = dict(_fk_fields_targeting("testapp", "PersonBase"))

    assert "personnotebase" in referencing
    assert "personnote" not in referencing


def test_fk_fields_targeting_ignores_models_pointing_elsewhere():
    referencing = _fk_fields_targeting("testapp", "PersonBase")

    assert not any(model_name.startswith("addressnote") for model_name, _ in referencing)


def test_fk_fields_targeting_is_empty_for_a_model_nothing_points_at():
    assert _fk_fields_targeting("testapp", "MetaTestBase") != []  # MetaTestNote points at it
    assert _fk_fields_targeting("testapp", "NoSuchModelBase") == []


def test_adding_the_soft_delete_flag_rebuilds_the_triggers_pointing_at_it():
    op = migrations.AddField(model_name="PersonBase", name="_overlay_deleted", field=models.BooleanField(default=False))

    extra_ops = extra_ops_for_migration("testapp", [op], {}, set())

    rebuilt = {(o.model_name, o.field_name) for o in extra_ops if isinstance(o, AddOverlayConstraint)}
    assert ("personnotebase", "person") in rebuilt
    assert ("personprofile", "person") in rebuilt


def test_removing_the_soft_delete_flag_rebuilds_them_too():
    op = migrations.RemoveField(model_name="PersonBase", name="_overlay_deleted")

    extra_ops = extra_ops_for_migration("testapp", [op], {}, set())

    rebuilt = {(o.model_name, o.field_name) for o in extra_ops if isinstance(o, AddOverlayConstraint)}
    assert ("personnotebase", "person") in rebuilt


def test_an_ordinary_field_change_rebuilds_nothing():
    op = migrations.AddField(model_name="PersonBase", name="nickname", field=models.CharField(max_length=1))

    extra_ops = extra_ops_for_migration("testapp", [op], {}, set())

    assert not [o for o in extra_ops if isinstance(o, AddOverlayConstraint)]


def test_the_rebuild_is_not_emitted_twice():
    ops = [
        migrations.AddField(model_name="PersonBase", name="_overlay_deleted", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="PersonBase", name="_overlay_deleted", field=models.BooleanField(default=False)),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, {}, set())

    rebuilt = [o for o in extra_ops if isinstance(o, AddOverlayConstraint)]
    assert len(rebuilt) == len({(o.model_name, o.field_name) for o in rebuilt})
