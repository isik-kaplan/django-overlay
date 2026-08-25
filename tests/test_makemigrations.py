from unittest import mock
from unittest.mock import patch

from django.db import migrations, models
from hypothesis import given
from hypothesis import strategies as st

from django_overlay.constraints import OverlayUniqueConstraint
from django_overlay.fields import OverlayForeignKey
from django_overlay.management.commands import makemigrations
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


class _StubMeta:
    def __init__(self, model_name, fields):
        self.model_name = model_name
        self._fields = fields

    def get_fields(self):
        return self._fields


class _StubModel:
    def __init__(self, model_name, fields):
        self._meta = _StubMeta(model_name, fields)


class _StubAppConfig:
    """Records how get_models was asked, and answers with what it is given."""

    def __init__(self, models_by_flag):
        self.models_by_flag = models_by_flag
        self.asked = []

    def get_models(self, include_auto_created=False):
        self.asked.append(include_auto_created)
        return self.models_by_flag.get(bool(include_auto_created), [])


def _uncontributed_overlay_fk(name, to="testapp.Address"):
    """An OverlayForeignKey that was never attached to a model.

    Django sets `concrete` when a field is contributed to a class, so a field
    that never was has no such attribute -- which is the case the getattr
    default exists to answer, and one the app registry cannot produce.
    """
    field = OverlayForeignKey(to, on_delete=models.CASCADE)
    field.name = name
    assert not hasattr(field, "concrete"), "the premise: this field is not concrete"
    return field


class _RecordingConfig:
    """The real app config, plus a note of how get_models was asked."""

    def __init__(self, real):
        self.real = real
        self.asked = []

    def __getattr__(self, name):
        return getattr(self.real, name)

    def get_models(self, include_auto_created=False):
        self.asked.append(include_auto_created)
        return self.real.get_models(include_auto_created=include_auto_created)


def test_the_registry_asks_for_auto_created_models_too():
    """`include_auto_created=True` is the argument that reaches through tables.

    No fixture here has an auto-created model -- OverlayManyToManyField requires
    an explicit through= -- so the flag cannot be observed in the result, and
    mutants of it survived on both helpers. What it asks for is asserted
    instead, which is the contract either way.
    """
    from django.apps import apps as real_apps

    config = _RecordingConfig(real_apps.get_app_config("testapp"))

    with mock.patch.object(makemigrations.django_apps, "get_app_config", return_value=config):
        _overlay_foreign_key_fields("testapp")
        _fk_fields_targeting("testapp", "PersonBase")

    assert config.asked[0] is True, "_overlay_foreign_key_fields must include auto-created models"
    assert True in config.asked[1:], "_fk_fields_targeting must too, when it looks for referrers"


def test_a_field_that_was_never_attached_to_a_model_is_ignored():
    """The `concrete` guard, which the registry cannot exercise either."""
    ghost = _uncontributed_overlay_fk("ghost")
    config = _StubAppConfig({True: [_StubModel("stub", [ghost])], False: []})

    with mock.patch.object(makemigrations.django_apps, "get_app_config", return_value=config):
        fields = _overlay_foreign_key_fields("testapp")

    assert fields == set(), "a field with no concrete attribute must not be registered"


def test_a_field_never_attached_to_a_model_is_ignored_when_looking_for_referrers():
    """The same guard on the other helper, which has its own copy of it.

    _fk_fields_targeting cannot reach its second loop unless the first one finds
    a target, so the stub adds the unattached field to the real models rather
    than replacing them.
    """
    from django.apps import apps as real_apps

    # Pointed at the very model being looked for, so the only thing that can
    # exclude it is the guard under test rather than the target filter below it.
    from tests.testapp.models import Person

    ghost = _uncontributed_overlay_fk("ghost")
    ghost.remote_field.model = Person
    ghost_model = _StubModel("ghost_model", [ghost])

    class _ConfigWithAGhost(_RecordingConfig):
        def get_models(self, include_auto_created=False):
            models = list(super().get_models(include_auto_created=include_auto_created))
            return models + [ghost_model] if include_auto_created else models

    config = _ConfigWithAGhost(real_apps.get_app_config("testapp"))

    with mock.patch.object(makemigrations.django_apps, "get_app_config", return_value=config):
        referencing = _fk_fields_targeting("testapp", "PersonBase")

    assert ("ghost_model", "ghost") not in referencing
    assert ("personnotebase", "person") in referencing, "the real referrers are still found"


def test_foreign_key_fields_includes_bonus_table_fks():
    fields = _overlay_foreign_key_fields("testapp")
    assert ("addressnote", "address") in fields
    assert ("personprofile", "person") in fields


def only(operations, kind, **attributes):
    """The one operation of `kind` matching `attributes`, app_label included.

    `assert any(isinstance(o, kind) and o.model_name == ...)` says nothing
    about which app the operation lands in, and every one of these operations
    takes an app_label as its first argument. A dozen mutants replaced that
    argument with None and lived. It also says nothing about how many matched,
    which is what makes a duplicated operation invisible.
    """
    matched = [
        operation
        for operation in operations
        if isinstance(operation, kind) and all(getattr(operation, name) == value for name, value in attributes.items())
    ]
    assert len(matched) == 1, f"expected exactly one {kind.__name__}{attributes}, got {len(matched)}"
    assert matched[0].app_label == "testapp", f"{kind.__name__} was given app_label {matched[0].app_label!r}"
    return matched[0]


def test_rename_field_on_a_base_model_triggers_a_view_resync():
    base_to_view = _overlay_base_to_view("testapp")
    op = migrations.RenameField(model_name="MetaTestBase", old_name="name", new_name="full_name")

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    only(extra_ops, SyncOverlayView, model_name="MetaTest")


def test_rename_model_on_a_base_model_triggers_a_view_resync():
    base_to_view = _overlay_base_to_view("testapp")
    op = migrations.RenameModel(old_name="PersonBase", new_name="ClientBase")

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    only(extra_ops, SyncOverlayView, model_name="Person")


def test_renaming_a_view_models_own_field_does_not_trigger_a_resync():
    base_to_view = _overlay_base_to_view("testapp")
    op = migrations.RenameField(model_name="MetaTest", old_name="name", new_name="full_name")

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    assert not any(isinstance(o, SyncOverlayView) for o in extra_ops)


def test_renaming_an_overlay_foreign_key_column_retriggers_its_constraint():
    fk_fields = {("addressnote", "address")}
    op = migrations.RenameField(model_name="AddressNote", old_name="addr", new_name="address")

    extra_ops = extra_ops_for_migration("testapp", [op], {}, fk_fields)

    only(extra_ops, AddOverlayConstraint, model_name="addressnote", field_name="address")


def test_adding_an_overlay_unique_constraint_to_an_existing_model_is_detected():
    base_to_view = _overlay_base_to_view("testapp")
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="fake_constraint")
    op = migrations.AddConstraint(model_name="UniqueTestBase", constraint=constraint)

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    only(extra_ops, AddOverlayUniqueConstraint, model_name="UniqueTest", constraint_name="fake_constraint")


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

    only(extra_ops, RemoveOverlayConstraint, model_name="MetaTest", field_name="some_plain_field")


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

    only(extra_ops, AddOverlayConstraint, model_name="AddressNote", field_name="address")


def test_creating_a_model_with_an_overlay_foreign_key_field_triggers_a_constraint():
    op = migrations.CreateModel(
        name="AddressNote",
        fields=[
            ("id", models.AutoField(primary_key=True)),
            ("address", OverlayForeignKey("Address", on_delete=models.CASCADE)),
        ],
    )

    extra_ops = extra_ops_for_migration("testapp", [op], {}, set())

    only(extra_ops, AddOverlayConstraint, model_name="AddressNote", field_name="address")


def test_adding_an_overlay_unique_constraint_to_a_brand_new_model_is_detected():
    base_to_view = _overlay_base_to_view("testapp")
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="fake_constraint")
    op = migrations.CreateModel(
        name="UniqueTestBase",
        fields=[("ssn", models.CharField(max_length=20))],
        options={"constraints": [constraint]},
    )

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    only(extra_ops, AddOverlayUniqueConstraint, model_name="UniqueTest", constraint_name="fake_constraint")


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


def test_create_model_then_add_field_still_only_adds_one_constraint():
    """The mirror of the test above, and it is not the same test.

    With CreateModel first, it is the key *that branch* records which stops the
    later AddField from adding a second constraint. Recording the wrong thing
    there is invisible in the other order, because the AddField branch had
    already recorded the right key.
    """
    ops = [
        migrations.CreateModel(name="Foo", fields=[("bar", OverlayForeignKey("Address", on_delete=models.CASCADE))]),
        migrations.AddField(model_name="Foo", name="bar", field=OverlayForeignKey("Address", on_delete=models.CASCADE)),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, {}, set())

    only(extra_ops, AddOverlayConstraint, model_name="Foo", field_name="bar")


def test_two_registry_fields_are_each_retriggered_once():
    """The retrigger branch keys on the registry pair, and one pair dedups
    against itself however the key is built. Two pairs and two operations are
    what make the key's identity observable."""
    fk_fields = {("addressnote", "address"), ("addressnote", "billing")}
    ops = [
        migrations.RenameField(model_name="AddressNote", old_name="addr", new_name="address"),
        migrations.RenameField(model_name="AddressNote", old_name="bill", new_name="billing"),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, {}, fk_fields)

    only(extra_ops, AddOverlayConstraint, model_name="addressnote", field_name="address")
    only(extra_ops, AddOverlayConstraint, model_name="addressnote", field_name="billing")


def test_a_field_pointing_at_a_registry_model_by_label_retriggers_it():
    """references_field resolves the target through the app label.

    Asking whether an operation touches ("address", "id") is answered by
    Django comparing the field's target against (app_label, name) -- so without
    the label the answer is always no, and altering a foreign key stops
    rebuilding the trigger on the model it points at.
    """
    op = migrations.AlterField(
        model_name="AddressNote",
        name="address",
        field=OverlayForeignKey("testapp.Address", on_delete=models.CASCADE),
    )

    extra_ops = extra_ops_for_migration("testapp", [op], {}, {("address", "id")})

    only(extra_ops, AddOverlayConstraint, model_name="address", field_name="id")


def test_the_same_new_unique_constraint_appearing_twice_only_adds_it_once():
    base_to_view = _overlay_base_to_view("testapp")
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="fake_constraint")
    ops = [
        migrations.AddConstraint(model_name="UniqueTestBase", constraint=constraint),
        migrations.AddConstraint(model_name="UniqueTestBase", constraint=constraint),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, base_to_view, set())

    assert len([o for o in extra_ops if isinstance(o, AddOverlayUniqueConstraint)]) == 1


# Dedup with a single key proves very little: collapse every key to the same
# value and one operation still comes out. These use two distinct keys, where
# a broken key makes the second one vanish.
def test_two_different_fk_fields_each_get_their_own_constraint():
    ops = [
        migrations.AddField(
            model_name="AddressNote", name="address", field=OverlayForeignKey("Address", on_delete=models.CASCADE)
        ),
        migrations.AddField(
            model_name="AddressNote", name="billing", field=OverlayForeignKey("Address", on_delete=models.CASCADE)
        ),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, {}, set())

    only(extra_ops, AddOverlayConstraint, model_name="AddressNote", field_name="address")
    only(extra_ops, AddOverlayConstraint, model_name="AddressNote", field_name="billing")


def test_two_different_base_models_each_get_their_own_view_resync():
    base_to_view = _overlay_base_to_view("testapp")
    ops = [
        migrations.RenameField(model_name="MetaTestBase", old_name="name", new_name="full_name"),
        migrations.RenameField(model_name="PersonBase", old_name="first_name", new_name="given_name"),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, base_to_view, set())

    only(extra_ops, SyncOverlayView, model_name="MetaTest")
    only(extra_ops, SyncOverlayView, model_name="Person")


def test_two_different_unique_constraints_are_both_added():
    base_to_view = _overlay_base_to_view("testapp")
    ops = [
        migrations.AddConstraint(
            model_name="UniqueTestBase",
            constraint=OverlayUniqueConstraint(fields=["ssn"], name="first_constraint"),
        ),
        migrations.AddConstraint(
            model_name="UniqueTestBase",
            constraint=OverlayUniqueConstraint(fields=["email"], name="second_constraint"),
        ),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, base_to_view, set())

    only(extra_ops, AddOverlayUniqueConstraint, model_name="UniqueTest", constraint_name="first_constraint")
    only(extra_ops, AddOverlayUniqueConstraint, model_name="UniqueTest", constraint_name="second_constraint")


def test_a_constraint_on_an_unknown_model_does_not_stop_the_ones_after_it():
    """`continue` and `break` differ only when something follows the skip."""
    base_to_view = _overlay_base_to_view("testapp")
    ops = [
        migrations.CreateModel(
            name="NotAnOverlayBase",
            fields=[("ssn", models.CharField(max_length=20))],
            options={"constraints": [OverlayUniqueConstraint(fields=["ssn"], name="ignored_constraint")]},
        ),
        migrations.AddConstraint(
            model_name="UniqueTestBase",
            constraint=OverlayUniqueConstraint(fields=["ssn"], name="wanted_constraint"),
        ),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, base_to_view, set())

    only(extra_ops, AddOverlayUniqueConstraint, model_name="UniqueTest", constraint_name="wanted_constraint")


def test_removing_a_constraint_names_the_app_and_the_view():
    base_to_view = _overlay_base_to_view("testapp")
    op = migrations.RemoveConstraint(model_name="UniqueTestBase", name="gone_constraint")

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    only(extra_ops, RemoveOverlayUniqueConstraint, model_name="UniqueTest", constraint_name="gone_constraint")


# The dedup guards need two operations landing on the *same* key. One key that
# collapses to None still dedups against itself; two operations that should
# produce one result are what makes the key's identity observable.
def test_the_same_view_is_only_synced_once_across_two_operations():
    """`synced_views.add(view_name)` -- with None added instead, the set never
    contains the name and every later operation re-adds the same sync."""
    base_to_view = _overlay_base_to_view("testapp")
    ops = [
        migrations.RenameField(model_name="MetaTestBase", old_name="name", new_name="full_name"),
        migrations.RenameField(model_name="MetaTestBase", old_name="full_name", new_name="title"),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, base_to_view, set())

    only(extra_ops, SyncOverlayView, model_name="MetaTest")


def test_a_field_referencing_a_model_by_label_still_syncs_its_view():
    """`op.references_model(base_name, app_label)` -- the label is what resolves
    "testapp.Person" to this app's Person.

    Django compares the field's target against (app_label, name), so with None
    passed instead the reference simply does not resolve, and adding a foreign
    key to an overlay model stops rebuilding that model's view.
    """
    base_to_view = _overlay_base_to_view("testapp")
    op = migrations.AddField(
        model_name="AddressNote",
        name="owner",
        field=models.ForeignKey("testapp.PersonBase", on_delete=models.CASCADE),
    )

    extra_ops = extra_ops_for_migration("testapp", [op], base_to_view, set())

    only(extra_ops, SyncOverlayView, model_name="Person")


def test_a_retriggered_fk_is_matched_by_label_too():
    """The same argument on references_field, for the retrigger branch."""
    op = migrations.AlterField(
        model_name="AddressNote",
        name="address",
        field=OverlayForeignKey("testapp.Address", on_delete=models.CASCADE),
    )

    extra_ops = extra_ops_for_migration("testapp", [op], {}, {("addressnote", "address")})

    only(extra_ops, AddOverlayConstraint, model_name="addressnote", field_name="address")


def test_removing_a_field_drops_the_view_for_the_right_app():
    """The RemoveField branch of the drop inserter has its own app_label."""
    op = migrations.RemoveField(model_name="SomeModel", name="gone")

    operations = _insert_view_drops_before_destructive_ops("testapp", [op])

    assert isinstance(operations[0], DropOverlayView)
    assert operations[0].app_label == "testapp"
    assert operations[0].model_name == "SomeModel"


def test_the_same_soft_delete_flip_only_rebuilds_each_trigger_once():
    """The _overlay_deleted branch keys on (model, field) and two operations in
    one migration both reach it."""
    ops = [
        migrations.AddField(model_name="PersonBase", name="_overlay_deleted", field=models.BooleanField(default=False)),
        migrations.RemoveField(model_name="PersonBase", name="_overlay_deleted"),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, {}, set())

    # Two distinct pairs, not one. A key that collapses to None still dedups
    # against itself, so a single expected constraint proves nothing -- the
    # second one vanishing is what makes the key's identity observable.
    only(extra_ops, AddOverlayConstraint, model_name="personnotebase", field_name="person")
    only(extra_ops, AddOverlayConstraint, model_name="personprofile", field_name="person")


def test_a_renamed_fk_column_is_retriggered_once_for_two_operations():
    """The retrigger branch keys on (model, field) from the registry."""
    fk_fields = {("addressnote", "address")}
    ops = [
        migrations.RenameField(model_name="AddressNote", old_name="addr", new_name="address"),
        migrations.AlterField(
            model_name="AddressNote",
            name="address",
            field=OverlayForeignKey("Address", on_delete=models.CASCADE),
        ),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, {}, fk_fields)

    only(extra_ops, AddOverlayConstraint, model_name="addressnote", field_name="address")


def test_the_unique_constraint_key_is_case_insensitive_on_the_model_name():
    """`(model_name.lower(), constraint.name)` -- AddConstraint carries the
    model name as written, and CreateModel carries it capitalised, so the same
    constraint arriving both ways has to land on one key."""
    base_to_view = _overlay_base_to_view("testapp")
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="dup_constraint")
    ops = [
        migrations.CreateModel(
            name="UniqueTestBase",
            fields=[("ssn", models.CharField(max_length=20))],
            options={"constraints": [constraint]},
        ),
        migrations.AddConstraint(model_name="uniquetestbase", constraint=constraint),
    ]

    extra_ops = extra_ops_for_migration("testapp", ops, base_to_view, set())

    only(extra_ops, AddOverlayUniqueConstraint, model_name="UniqueTest", constraint_name="dup_constraint")


def test_command_write_migration_files_appends_extra_ops_before_delegating():
    op = migrations.RenameField(model_name="MetaTestBase", old_name="name", new_name="full_name")
    migration = migrations.Migration("some_migration", "testapp")
    migration.operations = [op]
    changes = {"testapp": [migration]}

    with patch("django.core.management.commands.makemigrations.Command.write_migration_files") as base_write:
        Command().write_migration_files(changes)

    only(migration.operations, SyncOverlayView, model_name="MetaTest")
    base_write.assert_called_once_with(changes)


def test_write_migration_files_passes_djangos_own_arguments_through():
    """Django's write_migration_files takes update_previous_migration_paths.

    Nothing passed it, so *args and **kwargs could both be dropped from the
    call to super() with every test still green -- and squashmigrations, which
    is what supplies that argument, would silently stop updating the paths.
    """
    changes = {"testapp": []}

    with patch("django.core.management.commands.makemigrations.Command.write_migration_files") as base_write:
        Command().write_migration_files(changes, update_previous_migration_paths=True)
        Command().write_migration_files(changes, True)

    assert base_write.call_args_list[0].kwargs == {"update_previous_migration_paths": True}
    assert base_write.call_args_list[1].args == (changes, True)


def test_write_migration_files_labels_the_operations_with_their_own_app():
    """The app_label handed to both helpers comes from the changes dict, and
    nothing had asserted it survives the trip."""
    op = migrations.DeleteModel(name="MetaTestBase")
    migration = migrations.Migration("some_migration", "testapp")
    migration.operations = [op]

    with patch("django.core.management.commands.makemigrations.Command.write_migration_files"):
        Command().write_migration_files({"testapp": [migration]})

    labels = {o.app_label for o in migration.operations if hasattr(o, "app_label")}
    assert labels == {"testapp"}


def test_deleting_a_model_gets_a_view_drop_inserted_immediately_before_it():
    op = migrations.DeleteModel(name="SomeModel")

    operations = _insert_view_drops_before_destructive_ops("testapp", [op])

    assert len(operations) == 2
    assert isinstance(operations[0], DropOverlayView)
    assert operations[0].model_name == "SomeModel"
    # Which app the drop belongs to was never asserted, so the argument could
    # be replaced with None on both branches of this function and nothing
    # noticed -- the same gap the `only()` helper closes for the other
    # operations.
    assert operations[0].app_label == "testapp"
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


def test_fk_fields_targeting_takes_the_base_name_not_the_view_name():
    """It matches base models, and both halves of that filter matter.

    `isinstance(model, OverlayModelBase) and hasattr(model, "_view_model")`
    selects base models; with `or` between them it selects the view models too,
    which doubles the candidate set and makes the view's own name match. Passing
    a view name would then rebuild triggers for a model whose base is called
    something else.
    """
    assert _fk_fields_targeting("testapp", "Person") == []
    assert _fk_fields_targeting("testapp", "PersonBase") != []


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


def test_the_overridden_makemigrations_is_the_one_django_resolves():
    """Django resolves a command name to whichever app declares it last in
    INSTALLED_APPS. If another app overrides makemigrations too, the loser is
    silently unused — and if that were ours, view and trigger operations would
    quietly stop being generated. See docs/operations/MIGRATIONS.md."""
    from django.core.management import get_commands

    assert get_commands()["makemigrations"] == "django_overlay"
