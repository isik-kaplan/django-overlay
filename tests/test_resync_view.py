import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_overlay.sync import resync_view
from tests.testapp.models import CURRENT_PROVIDER, SwitchableSourceTest
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
