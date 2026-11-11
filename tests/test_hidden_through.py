"""OverlayManyToManyField without through=: the through model it builds, the
ORM over it, its integrity triggers, and its life in migrations.

The test app's models are HiddenPerson (overlay) with `tags` to a plain model,
`addresses` to another overlay model and `friends` to itself, and HiddenTeam
(plain) with `members` to HiddenPerson. Migrations 0038-0040 create them,
add two more many-to-manys, then remove those again."""

import pytest
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, migrations, models, transaction
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.optimizer import MigrationOptimizer
from django.db.migrations.state import ModelState, ProjectState
from django.forms import modelform_factory

from django_overlay.autodetector import refuse_dropping_a_through_table, with_hidden_throughs
from django_overlay.fields import OverlayForeignKey
from django_overlay.models import OverlayMeta, OverlayModel
from django_overlay.operations import AddOverlayConstraint, RemoveOverlayConstraint, RemoveOverlayConstraintsOf
from django_overlay.sources import SourceTable
from tests.testapp.models import Address, HiddenPerson, HiddenTag, HiddenTeam


def _through(model, name):
    return model._meta.get_field(name).remote_field.through


def _links(through):
    return {field.name: type(field) for field in through._meta.local_fields if field.is_relation}


# --- the model it builds ------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "name", "table", "links"),
    [
        (
            HiddenPerson,
            "tags",
            "hiddenperson_tags",
            {"hiddenperson": OverlayForeignKey, "hiddentag": models.ForeignKey},
        ),
        (
            HiddenPerson,
            "addresses",
            "hiddenperson_addresses",
            {"hiddenperson": OverlayForeignKey, "address": OverlayForeignKey},
        ),
        (
            HiddenPerson,
            "friends",
            "hiddenperson_friends",
            {"from_hiddenperson": OverlayForeignKey, "to_hiddenperson": OverlayForeignKey},
        ),
        (
            HiddenTeam,
            "members",
            "testapp_hiddenteam_members",
            {"hiddenteam": models.ForeignKey, "hiddenperson": OverlayForeignKey},
        ),
    ],
)
def test_each_end_at_an_overlay_model_is_an_overlay_foreign_key(model, name, table, links):
    """Table and column names are Django's for the same field, except that an
    overlay model's table is its base table, not its view."""
    through = _through(model, name)

    assert {k: v for k, v in _links(through).items()} == links
    assert through._meta.db_table == table
    assert through._meta.auto_created is model
    for field in through._meta.local_fields:
        if type(field) is models.ForeignKey:
            assert field.db_constraint


def test_the_built_through_model_is_named_in_the_fields_deconstruction():
    """In migration state it is a model of its own, so the field's historical
    copy has to point at it rather than build a second one."""
    _, path, _, kwargs = HiddenTeam._meta.get_field("members").deconstruct()

    assert path == "django_overlay.fields.OverlayManyToManyField"
    assert kwargs["through"] == "testapp.HiddenTeam_members"


def test_an_explicit_through_model_is_left_as_given():
    from tests.testapp.models import Person, PersonAddressThrough

    field = Person._meta.get_field("addresses")

    assert field.remote_field.through is PersonAddressThrough
    assert not getattr(field, "_builds_its_through", False)
    assert field.deconstruct()[3]["through"] == "testapp.PersonAddressThrough"


def test_its_through_is_known_by_name_before_it_is_built():
    """Django resolves a string through= when both ends are registered;
    until then, deconstruct names it as the string it is."""
    field = OverlayManyToManyField_unbuilt()

    assert field.deconstruct()[3]["through"] == "testapp.Unbuilt_tags"


def OverlayManyToManyField_unbuilt():
    from django.test.utils import isolate_apps

    from django_overlay.fields import OverlayManyToManyField

    with isolate_apps("tests.testapp"):
        field = OverlayManyToManyField("NeverDeclared", blank=True)
        type(
            "Unbuilt",
            (models.Model,),
            {"__module__": __name__, "Meta": type("Meta", (), {"app_label": "testapp"}), "tags": field},
        )
        return field


def test_no_check_complains_about_it():
    from django.core import checks

    errors = [
        error for error in checks.run_checks() if error.id.startswith(("django_overlay.", "fields.", "commands."))
    ]

    assert errors == []


def test_a_plain_many_to_many_to_an_overlay_model_is_still_refused():
    from django.test.utils import isolate_apps

    from django_overlay.checks import check_no_plain_fk_to_overlay_models

    with isolate_apps("tests.testapp") as registry:
        type(
            "PlainLinks",
            (models.Model,),
            {
                "__module__": __name__,
                "Meta": type("Meta", (), {"app_label": "testapp"}),
                "people": models.ManyToManyField(HiddenPerson, related_name="+"),
            },
        )
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("django_overlay.checks.apps", registry)
            ids = {error.id for error in check_no_plain_fk_to_overlay_models(None)}

    assert "django_overlay.E002" in ids


def test_a_plain_many_to_many_declared_on_an_overlay_model_points_at_the_field_class():
    """Its auto-created through has a plain ForeignKey to the view (E001), and
    the fix is the field's class, not the through model nobody wrote."""
    from django.test.utils import isolate_apps

    from django_overlay.checks import check_no_plain_fk_to_overlay_models

    with isolate_apps("tests.testapp") as registry:

        class PlainM2MOnOverlay(OverlayModel):
            tags = models.ManyToManyField(HiddenTag, related_name="+")

            class Meta:
                app_label = "testapp"

            class OverlayMeta(OverlayMeta.with_strategy(OverlayModel.Strategy.NEGATIVE_ID)):
                @staticmethod
                def get_source():
                    return SourceTable(schema="public", table="testapp_shared_hiddenpersonsource")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("django_overlay.checks.apps", registry)
            hints = [error.hint for error in check_no_plain_fk_to_overlay_models(None) if error.id.endswith("E001")]

    assert hints and all("make that field a django_overlay.fields.OverlayManyToManyField" in hint for hint in hints)


# --- the ORM over it ------------------------------------------------------------


@pytest.mark.django_db
def test_every_arrangement_round_trips_through_the_orm():
    tag = HiddenTag.objects.create(label="t")
    address = Address.objects.create(street="1 Main")
    alice, bob = HiddenPerson.objects.create(name="alice"), HiddenPerson.objects.create(name="bob")
    team = HiddenTeam.objects.create(name="team")

    alice.tags.add(tag)
    alice.addresses.add(address)
    alice.friends.add(bob)
    team.members.add(alice, bob)

    assert list(tag.hiddenperson_set.all()) == [alice]
    assert list(address.hidden_people.all()) == [alice]
    assert list(bob.friends.all()) == [alice]  # symmetrical, as Django's own
    assert set(alice.teams.all()) == {team}
    assert set(HiddenPerson.objects.filter(tags__label="t")) == {alice}

    team.members.remove(bob)
    alice.tags.set([])
    assert list(team.members.all()) == [alice]
    assert not alice.tags.exists()


@pytest.mark.django_db
def test_linking_a_source_row_leaves_it_in_the_source():
    """The point of an OverlayForeignKey end: a row only the source holds can
    be linked without being copied into the base table first."""
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO testapp_shared_hiddenpersonsource (id, name) VALUES (41, 'from source')")
    person = HiddenPerson.objects.source_only().get()
    tag = HiddenTag.objects.create(label="t")

    person.tags.add(tag)

    assert HiddenPerson.objects.base_only().count() == 0
    assert list(tag.hiddenperson_set.all()) == [person]


@pytest.mark.django_db
def test_a_link_to_a_row_the_view_does_not_show_is_refused(db_cursor):
    tag = HiddenTag.objects.create(label="t")

    with pytest.raises(IntegrityError), transaction.atomic():
        _through(HiddenPerson, "tags").objects.create(hiddenperson_id=-999999, hiddentag=tag)
        db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.django_db
def test_a_model_form_edits_it_like_a_plain_many_to_many():
    """Django's forms and admin, and DRF, read auto_created to decide whether a
    many-to-many is editable; an explicit through= makes it read-only there."""
    alice = HiddenPerson.objects.create(name="alice")
    form = modelform_factory(HiddenTeam, fields=["name", "members"])(data={"name": "t", "members": [alice.pk]})

    assert form.is_valid(), form.errors
    team = form.save()
    assert list(team.members.all()) == [alice]


@pytest.mark.django_db(transaction=True)
def test_flush_empties_the_through_tables_of_an_overlay_model():
    """Django finds a many-to-many's table through its declaring model, and
    skips an unmanaged one; flush would otherwise leave hiddenperson_tags full
    and refuse to truncate testapp_hiddentag, which it references."""
    tag = HiddenTag.objects.create(label="t")
    HiddenPerson.objects.create(name="alice").tags.add(tag)

    tables = connection.introspection.django_table_names(only_existing=True, include_views=False)
    assert {"hiddenperson_tags", "hiddenperson_addresses", "hiddenperson_friends"} <= set(tables)
    assert "testapp_hiddenteam_members" in tables

    call_command("flush", verbosity=0, interactive=False)

    assert _through(HiddenPerson, "tags").objects.count() == 0
    assert HiddenTag.objects.count() == 0


# --- migrations ---------------------------------------------------------------


def _state_at(migration_name):
    return MigrationLoader(connection).project_state(("testapp", migration_name))


@pytest.mark.django_db
def test_historical_state_holds_the_through_models_as_models_of_their_own():
    state = _state_at("0038_hidden_through_tables")
    historical = state.apps

    through = historical.get_model("testapp", "HiddenPerson_tags")
    assert not through._meta.auto_created
    assert isinstance(through._meta.get_field("hiddenperson"), OverlayForeignKey)
    assert type(through._meta.get_field("hiddentag")) is models.ForeignKey
    members = historical.get_model("testapp", "HiddenTeam")._meta.get_field("members")
    assert members.remote_field.through is historical.get_model("testapp", "HiddenTeam_members")


@pytest.mark.django_db
def test_the_tables_and_triggers_exist_after_migrating_from_zero(db_cursor):
    db_cursor.execute(
        "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgrelid IN "
        "('hiddenperson_tags'::regclass, 'hiddenperson_friends'::regclass, 'testapp_hiddenteam_members'::regclass)"
    )
    names = {row[0] for row in db_cursor.fetchall()}

    assert names == {
        "overlayfk_hiddenperson_tags_hiddenperson_id",
        "overlayfk_hiddenperson_friends_from_hiddenperson_id",
        "overlayfk_hiddenperson_friends_to_hiddenperson_id",
        "overlayfk_testapp_hiddenteam_members_hiddenperson_id",
    }


@pytest.mark.django_db
def test_removing_a_many_to_many_drops_its_table_and_every_trigger_naming_it(db_cursor):
    """0040 removes HiddenPerson.former_tags and HiddenTeam.alumni. Their
    delete-side triggers live on hiddenperson's base table; left behind, the
    next hard delete of a person would fail on a table that is gone."""
    db_cursor.execute("SELECT to_regclass('hiddenperson_former_tags'), to_regclass('testapp_hiddenteam_alumni')")
    assert db_cursor.fetchone() == (None, None)
    db_cursor.execute("SELECT tgname FROM pg_trigger WHERE tgname LIKE '%%former_tags%%' OR tgname LIKE '%%alumni%%'")
    assert db_cursor.fetchall() == []

    person = HiddenPerson.objects.create(name="organic")
    person.delete()

    assert not HiddenPerson.objects.filter(pk=person.pk).exists()


@pytest.mark.django_db
def test_the_removal_migration_drops_triggers_while_their_model_is_still_in_state():
    """Django removes a deleted model's relations before deleting it, so each
    trigger drop has to come before the RemoveField it belongs to, and before
    the DeleteModel when there is no RemoveField."""
    migration = MigrationLoader(connection).get_migration("testapp", "0040_remove_m2ms")
    ops = migration.operations

    for i, op in enumerate(ops):
        if isinstance(op, migrations.RemoveField):
            assert isinstance(ops[i - 1], RemoveOverlayConstraint)
            assert (ops[i - 1].model_name, ops[i - 1].field_name) == (op.model_name, op.name)
        if isinstance(op, migrations.DeleteModel):
            assert isinstance(ops[i - 1], RemoveOverlayConstraintsOf)
            assert ops[i - 1].model_name == op.name


def test_a_squash_keeps_every_trigger_operation_in_order():
    """squashmigrations runs the optimizer over the migrations' operations. The
    trigger operations are RunPython and not elidable, so a CreateModel and the
    DeleteModel of the same through model can't be folded across them."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    names = ["0038_hidden_through_tables", "0039_add_m2ms_to_remove", "0040_remove_m2ms"]
    ops = [op for name in names for op in loader.get_migration("testapp", name).operations]

    optimized = MigrationOptimizer().optimize(ops, "testapp")

    def runpython(sequence):
        return [(type(op).__name__, op.deconstruct()[1]) for op in sequence if isinstance(op, migrations.RunPython)]

    assert runpython(optimized) == runpython(ops)
    assert any(isinstance(op, AddOverlayConstraint) for op in optimized)


@pytest.mark.django_db
def test_makemigrations_and_migrate_see_no_missing_migration():
    """Both build the current models' state with ProjectState.from_apps, which
    holds the through models; migrate would otherwise report them deleted on
    every run. Neither command's autodetector is replaced, so Django's
    commands.E001 holds whoever's migrate a project has."""
    from django.core import checks

    assert ("testapp", "hiddenperson_tags") in ProjectState.from_apps(apps).models
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)
    assert not [error for error in checks.run_checks(tags=["commands"]) if error.id == "commands.E001"]


def _changes_replacing_an_explicit_through():
    """Migration state as if HiddenPerson.tags had an explicit through model on
    the same table, and what Django generates for switching to the built one."""
    after = ProjectState.from_apps(apps)
    before = ProjectState.from_apps(apps)
    before.remove_model("testapp", "hiddenperson_tags")
    before.add_model(
        ModelState(
            "testapp",
            "PersonTagLink",
            [
                ("id", models.AutoField(primary_key=True)),
                ("hiddenperson", OverlayForeignKey("testapp.HiddenPerson", models.CASCADE)),
                ("hiddentag", models.ForeignKey("testapp.HiddenTag", models.CASCADE)),
            ],
            options={"db_table": "hiddenperson_tags"},
        )
    )
    return before, MigrationAutodetector(before, after).changes(graph=_graph())


def test_makemigrations_refuses_to_drop_a_table_a_built_through_model_takes_over():
    before, changes = _changes_replacing_an_explicit_through()

    with pytest.raises(CommandError, match="drops hiddenperson_tags and creates it again, empty"):
        refuse_dropping_a_through_table(before, changes)


def test_the_refusal_is_what_makemigrations_runs_before_writing():
    from unittest import mock

    from django_overlay.management.commands.makemigrations import Command

    before, changes = _changes_replacing_an_explicit_through()
    with (
        mock.patch("django_overlay.management.commands.makemigrations.MigrationLoader") as loader,
        mock.patch("django.core.management.commands.makemigrations.Command.write_migration_files") as write,
        pytest.raises(CommandError, match="creates it again, empty"),
    ):
        loader.return_value.project_state.return_value = before
        Command().write_migration_files(changes)
    write.assert_not_called()


def _graph():
    return MigrationLoader(None, ignore_no_migrations=True).graph


@pytest.mark.django_db
def test_a_state_that_does_not_hold_the_declaring_model_is_left_alone():
    state = ProjectState()

    assert with_hidden_throughs(state).models == {}


def test_a_new_model_that_is_no_through_does_not_trip_the_refusal():
    before = ProjectState.from_apps(apps)
    after = ProjectState.from_apps(apps)
    after.add_model(ModelState("testapp", "BrandNew", [("id", models.AutoField(primary_key=True))]))
    changes = MigrationAutodetector(before, after).changes(graph=_graph())
    changes["testapp"][0].operations.append(migrations.DeleteModel("NotInState"))

    refuse_dropping_a_through_table(before, changes)

    assert [op.name for op in changes["testapp"][0].operations] == ["BrandNew", "NotInState"]


def test_a_through_already_in_state_is_not_added_twice():
    state = with_hidden_throughs(ProjectState.from_apps(apps))
    count = len(state.models)

    assert len(with_hidden_throughs(state).models) == count


def test_the_fields_own_db_table_names_the_through_table():
    from django.test.utils import isolate_apps

    from django_overlay.fields import OverlayManyToManyField

    with isolate_apps("tests.testapp"):
        meta = type("Meta", (), {"app_label": "testapp"})
        target = type("Target", (models.Model,), {"__module__": __name__, "Meta": meta})
        model = type(
            "OwnTable",
            (models.Model,),
            {
                "__module__": __name__,
                "Meta": type("Meta", (), {"app_label": "testapp"}),
                "targets": OverlayManyToManyField(target, db_table="chosen_name", related_name="+"),
            },
        )

        assert _through(model, "targets")._meta.db_table == "chosen_name"


def test_the_table_list_without_only_existing_names_them_too():
    tables = connection.introspection.django_table_names()

    assert "hiddenperson_tags" in tables


def test_the_wrappers_are_applied_once():
    from django.db.backends.base.introspection import BaseDatabaseIntrospection

    from django_overlay.apps import _list_hidden_through_tables, _put_hidden_throughs_in_state

    tables, from_apps = BaseDatabaseIntrospection.django_table_names, ProjectState.from_apps
    _list_hidden_through_tables()
    _put_hidden_throughs_in_state()

    assert BaseDatabaseIntrospection.django_table_names is tables
    assert ProjectState.from_apps == from_apps


@pytest.mark.django_db
def test_a_remove_overlay_constraint_written_before_0_4_0_still_runs(db_cursor):
    """0.3.0 and earlier appended it to the end of the migration, after its
    RemoveField; a model a migration deleted is gone from state there too, so
    it looks the model up live, as it always did."""

    class NoModels:
        def get_model(self, app_label, model_name):
            raise LookupError(model_name)

    with connection.schema_editor() as editor:
        RemoveOverlayConstraint("testapp", "AddressNote", "address").code(NoModels(), editor)
        statements = editor.deferred_sql

    assert statements == []
    db_cursor.execute("SELECT count(*) FROM pg_trigger WHERE tgname = 'overlayfk_testapp_addressnote_address_id'")
    assert db_cursor.fetchone()[0] == 0


@pytest.mark.django_db
def test_the_through_models_get_no_permissions():
    """Django gives its own through models none; in migration state these are
    ordinary models, which post_migrate would otherwise give four each."""
    from django.contrib.auth.models import Permission

    assert not Permission.objects.filter(content_type__app_label="testapp", codename__contains="hiddenperson_").exists()
    assert not Permission.objects.filter(codename__contains="hiddenteam_members").exists()
    assert Permission.objects.filter(codename="add_hiddenteam").exists()
