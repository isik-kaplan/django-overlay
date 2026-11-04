import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_overlay.sync import resync_view
from tests.testapp.models import _PROVIDER_TABLES, CURRENT_PROVIDER, SwitchableSourceTest
from tests.testapp_shared.models import ProviderAPersonSource, ProviderBPersonSource


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_provider():
    yield
    CURRENT_PROVIDER["value"] = "provider_a"


def test_resync_view_repoints_the_view_at_a_newly_selected_source():
    CURRENT_PROVIDER["value"] = "provider_a"
    resync_view(SwitchableSourceTest)
    provider_a_row = ProviderAPersonSource.objects.create(first_name="From Provider A")
    view_id = -provider_a_row.id
    assert SwitchableSourceTest.objects.get(id=view_id).first_name == "From Provider A"

    CURRENT_PROVIDER["value"] = "provider_b"
    resync_view(SwitchableSourceTest)
    # The id is given rather than left to the sequence. The point is that the
    # *same* view id resolves through the new source, which needs the same
    # underlying source id -- and the two providers have separate sequences.
    # Left to chance it passed only because other tests happened to keep the
    # two in step; run on its own, as mutmut runs a traced subset, the ids
    # diverged and the row was not found.
    ProviderBPersonSource.objects.create(id=provider_a_row.id, first_name="From Provider B")

    # Same view id, now resolving through the new source — proves the view
    # was rebuilt against it, not just unioned with the old one.
    assert SwitchableSourceTest.objects.get(id=view_id).first_name == "From Provider B"


def test_resync_overlay_views_management_command_does_the_same():
    CURRENT_PROVIDER["value"] = "provider_b"
    call_command("resync_overlay_views", "testapp.SwitchableSourceTest")
    row = ProviderBPersonSource.objects.create(first_name="Via Command")
    assert SwitchableSourceTest.objects.get(id=-row.id).first_name == "Via Command"


def test_resync_overlay_views_rejects_a_malformed_label():
    with pytest.raises(CommandError):
        call_command("resync_overlay_views", "not-a-valid-label")


def test_resync_overlay_views_rejects_an_unknown_model():
    with pytest.raises(CommandError):
        call_command("resync_overlay_views", "testapp.NotARealModel")


def test_the_command_advertises_its_argument_shape():
    """The metavar is the only place the expected form is spelled out, and it
    had five mutants: garbled, lowercased, dropped entirely."""
    from django_overlay.management.commands.resync_overlay_views import Command

    printed = " ".join(Command().create_parser("manage.py", "resync_overlay_views").format_help().split())

    assert "app_label.ModelName [app_label.ModelName ...]" in printed
    assert "--database DATABASE" in printed


def test_a_malformed_label_says_what_was_expected_and_what_it_got():
    """`pytest.raises(CommandError)` alone passes for any message at all,
    including None, which is what two mutants replaced it with."""
    with pytest.raises(CommandError) as raised:
        call_command("resync_overlay_views", "not-a-valid-label")

    assert str(raised.value) == "Expected app_label.ModelName, got 'not-a-valid-label'"


def test_an_unknown_model_reports_djangos_own_lookup_error():
    with pytest.raises(CommandError) as raised:
        call_command("resync_overlay_views", "testapp.NotARealModel")

    assert str(raised.value) == "App 'testapp' doesn't have a 'NotARealModel' model."


def test_the_database_option_reaches_resync_view():
    """`resync_view(model, using=options["database"])` -- drop the keyword and
    every resync silently goes to the default alias, which is exactly the bug
    a --database flag exists to prevent."""
    from unittest import mock

    from django_overlay.management.commands import resync_overlay_views as command_module

    with mock.patch.object(command_module, "resync_view") as resync:
        call_command("resync_overlay_views", "testapp.SwitchableSourceTest", database="other")

    resync.assert_called_once_with(SwitchableSourceTest, using="other")


def test_sync_view_carries_the_models_pk_default_sql_into_the_insert_trigger():
    """OverlayMeta.pk_default_sql was read and forwarded by sync_view with
    nothing asserting either step: no test model sets it, so replacing it with
    None -- at the assignment or at the call site, which is two mutants --
    changed nothing observable. The builder's own handling of the argument is
    covered in test_sql.py; what is covered here is that the model's value
    reaches it at all."""
    from unittest import mock

    from django_overlay.sync import sync_view

    statements = []
    with mock.patch.object(SwitchableSourceTest._overlay_meta, "pk_default_sql", "testapp_custom_uuid()"):
        sync_view(SwitchableSourceTest, "public", statements.append)

    insert_triggers = [sql for sql in statements if "_instead_of_insert" in sql]
    assert len(insert_triggers) == 1
    assert "testapp_custom_uuid()" in insert_triggers[0]
    # The strategy default would otherwise supply this one, so its absence is
    # what proves the model's value won.
    assert "nextval" not in insert_triggers[0]


def test_a_resync_keeps_the_column_order_the_view_already_has():
    """A view built while replaying migrations can order its columns
    differently from the live model: makemigrations moves a new model's foreign
    keys into AddField operations after its CreateModel. CREATE OR REPLACE VIEW
    cannot move a column, so a resync in the live order failed outright with
    "cannot change name of view column"."""
    from django.db import connection

    from django_overlay.introspection import view_columns
    from django_overlay.sync import resolve_schema, statement_executor, sync_view

    live = [f.column for f in SwitchableSourceTest._meta.fields]
    reordered = [*live[1:], live[0]]
    schema = resolve_schema(connection)
    with connection.cursor() as cursor:
        # Postgres will not reorder an existing view either, so build it afresh.
        cursor.execute(f'DROP VIEW "{SwitchableSourceTest._meta.db_table}"')
        sync_view(SwitchableSourceTest, schema, statement_executor(cursor), columns=reordered)

    resync_view(SwitchableSourceTest)

    with connection.cursor() as cursor:
        assert view_columns(cursor, schema, SwitchableSourceTest._meta.db_table)[1:] == reordered


@pytest.fixture
def text_typed_source():
    """A source whose first_name is text, where the model's column is varchar(100)."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE public.text_typed_person_source (id serial PRIMARY KEY, first_name text)")
    _PROVIDER_TABLES["text_typed"] = "text_typed_person_source"
    yield
    del _PROVIDER_TABLES["text_typed"]


def test_a_source_with_different_column_types_replaces_the_view(text_typed_source):
    """CREATE OR REPLACE VIEW cannot change a column's type, and a union with a
    text column turns varchar(100) into text. The view is dropped and built
    again instead, triggers and all."""
    from django.db import connection

    CURRENT_PROVIDER["value"] = "text_typed"
    resync_view(SwitchableSourceTest)
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO public.text_typed_person_source (id, first_name) VALUES (7, 'Typed')")

    assert SwitchableSourceTest.objects.get(id=-7).first_name == "Typed"
    created = SwitchableSourceTest.objects.create(first_name="Ours")
    assert SwitchableSourceTest.objects.get(pk=created.pk).first_name == "Ours"

    CURRENT_PROVIDER["value"] = "provider_a"
    resync_view(SwitchableSourceTest)
    assert not SwitchableSourceTest.objects.filter(id=-7).exists()


def test_a_view_built_on_it_stops_the_replacement_instead_of_disappearing(text_typed_source):
    from django.db import DatabaseError, connection

    with connection.cursor() as cursor:
        cursor.execute(f"CREATE VIEW public.on_top AS SELECT id FROM public.{SwitchableSourceTest._meta.db_table}")
    CURRENT_PROVIDER["value"] = "text_typed"

    with pytest.raises(DatabaseError):
        resync_view(SwitchableSourceTest)

    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.on_top') IS NOT NULL")
        assert cursor.fetchone()[0]


def test_the_refusal_is_recognised_under_psycopg2_too(monkeypatch):
    """psycopg2 reports the SQLSTATE as `pgcode`, psycopg 3 as `sqlstate`."""
    from django.db import DatabaseError

    from django_overlay import sync

    class Psycopg2Error(Exception):
        pgcode = "42P16"

    executed = []

    def execute(sql):
        executed.append(sql)
        if len(executed) == 1:
            error = DatabaseError("cannot change data type of view column")
            error.__cause__ = Psycopg2Error()
            raise error

    sync._drop_view_if_its_columns_change_type(SwitchableSourceTest, "public", execute, ["id", "first_name"], "default")

    assert "DROP VIEW" in executed[-1]
    assert "CASCADE;" not in executed[-1]


def test_any_other_refusal_of_the_new_view_is_raised():
    """Only a column-type change (42P16) is answered by dropping the view; any
    other error from the new view's SQL leaves the view alone and reaches the
    caller."""
    from django.db import DatabaseError

    from django_overlay import sync

    class SyntaxErrorCause(Exception):
        sqlstate = "42601"

    executed = []

    def execute(sql):
        executed.append(sql)
        error = DatabaseError("syntax error")
        error.__cause__ = SyntaxErrorCause()
        raise error

    with pytest.raises(DatabaseError, match="syntax error"):
        sync._drop_view_if_its_columns_change_type(
            SwitchableSourceTest, "public", execute, ["id", "first_name"], "default"
        )

    assert len(executed) == 1
    assert "DROP VIEW" not in executed[0]
