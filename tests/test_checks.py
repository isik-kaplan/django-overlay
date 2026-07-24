import os
import subprocess
import sys

import pytest

from django_overlay.checks import check_no_plain_fk_to_overlay_models


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


def test_overlay_foreign_key_itself_is_not_flagged():
    errors = check_no_plain_fk_to_overlay_models(None)
    assert not any("AddressNote.address" in e.msg for e in errors)


def test_overlay_many_to_many_field_with_its_required_through_is_not_flagged():
    errors = check_no_plain_fk_to_overlay_models(None)
    assert not any("Person.addresses" in e.msg or "Person.phones" in e.msg for e in errors)
    assert not any("PersonAddressThrough" in e.msg for e in errors)
    assert not any("PhoneTag.phones" in e.msg for e in errors)
