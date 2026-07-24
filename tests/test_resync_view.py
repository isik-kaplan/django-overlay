import pytest
from django.core.management import call_command

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
    ProviderBPersonSource.objects.create(first_name="From Provider B")

    # Same view id, now resolving through the new source — proves the view
    # was rebuilt against it, not just unioned with the old one.
    assert SwitchableSourceTest.objects.get(id=view_id).first_name == "From Provider B"


def test_resync_overlay_views_management_command_does_the_same():
    CURRENT_PROVIDER["value"] = "provider_b"
    call_command("resync_overlay_views", "testapp.SwitchableSourceTest")
    row = ProviderBPersonSource.objects.create(first_name="Via Command")
    assert SwitchableSourceTest.objects.get(id=-row.id).first_name == "Via Command"


def test_resync_overlay_views_rejects_a_malformed_label():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("resync_overlay_views", "not-a-valid-label")
