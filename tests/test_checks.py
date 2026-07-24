import os
import subprocess
import sys

import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import models

from django_overlay.checks import check_no_plain_fk_to_overlay_models
from tests.testapp.models import Address, Person


@pytest.fixture(scope="module")
def bad_fixtures_boot_result():
    """Boots Django with tests.bad_fixtures_app installed, in a subprocess
    so its expected ImproperlyConfigured crash doesn't take out this test
    process too."""
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "tests.bad_fixtures_settings"}
    return subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        env=env,
        capture_output=True,
        text=True,
    )


def test_appconfig_ready_refuses_to_boot_with_a_plain_fk_to_an_overlay_view_model(bad_fixtures_boot_result):
    assert bad_fixtures_boot_result.returncode != 0
    assert "django_overlay.E001" in bad_fixtures_boot_result.stderr
    assert "BadNote.person" in bad_fixtures_boot_result.stderr


def test_appconfig_ready_refuses_to_boot_with_a_plain_many_to_many_to_an_overlay_view_model(
    bad_fixtures_boot_result,
):
    assert "django_overlay.E002" in bad_fixtures_boot_result.stderr
    assert "BadManyToMany.addresses" in bad_fixtures_boot_result.stderr


def test_appconfig_ready_refuses_to_boot_with_the_bad_many_to_manys_auto_created_through_table(
    bad_fixtures_boot_result,
):
    matching_e001_lines = [
        line for line in bad_fixtures_boot_result.stderr.splitlines() if "django_overlay.E001" in line
    ]
    assert any("BadManyToMany" in line for line in matching_e001_lines)


def test_ready_raises_improperly_configured_for_a_bad_fk_and_m2m():
    # Same assertions as the subprocess tests above, but in-process: ready()
    # already ran once at startup, so calling it again here doesn't repeat
    # that — it just re-scans the (now-bad) registry, which is enough to
    # prove the raise itself and cover the E001/E002 branches directly.
    class TransientBadNote(models.Model):
        person = models.ForeignKey(Person, on_delete=models.DO_NOTHING, related_name="+")

        class Meta:
            app_label = "testapp"
            managed = False

    class TransientBadManyToMany(models.Model):
        addresses = models.ManyToManyField(Address, related_name="+")

        class Meta:
            app_label = "testapp"
            managed = False

    with pytest.raises(ImproperlyConfigured) as exc_info:
        apps.get_app_config("django_overlay").ready()

    message = str(exc_info.value)
    assert "django_overlay.E001" in message
    assert "TransientBadNote.person" in message
    assert "django_overlay.E002" in message
    assert "TransientBadManyToMany.addresses" in message
    # The M2M's own auto-created through table gets flagged too (E001).
    assert "TransientBadManyToMany_addresses.address" in message


def test_overlay_foreign_key_itself_is_not_flagged():
    errors = check_no_plain_fk_to_overlay_models(None)
    assert not any("AddressNote.address" in e.msg for e in errors)


def test_overlay_many_to_many_field_with_its_required_through_is_not_flagged():
    errors = check_no_plain_fk_to_overlay_models(None)
    assert not any("Person.addresses" in e.msg or "Person.phones" in e.msg for e in errors)
    assert not any("PersonAddressThrough" in e.msg for e in errors)
    assert not any("PhoneTag.phones" in e.msg for e in errors)
