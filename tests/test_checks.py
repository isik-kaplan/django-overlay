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


def test_appconfig_ready_refuses_to_boot_with_unsupported_uniqueness(bad_fixtures_boot_result):
    """ready() is the hard stop: it runs inside django.setup(), which every
    Django process must complete, and --skip-checks doesn't reach it."""
    assert bad_fixtures_boot_result.returncode != 0
    assert "django_overlay.E003" in bad_fixtures_boot_result.stderr
    assert "BadUniqueness declares uniqueness django_overlay can't honour" in bad_fixtures_boot_result.stderr


def test_the_boot_failure_carries_the_constraints_to_write_instead(bad_fixtures_boot_result):
    stderr = bad_fixtures_boot_result.stderr

    assert "- Meta.unique_together = ['first_name', 'last_name']" in stderr
    assert "- Meta.constraints has a plain UniqueConstraint 'bad_email_uniq'" in stderr
    assert "- email declares unique=True" in stderr
    assert "- desk is a OneToOneField" in stderr
    assert (
        'OverlayUniqueConstraint(fields=["first_name", "last_name"], name="baduniqueness_first_name_last_name_uniq")'
        in stderr
    )


def test_a_valid_overlay_model_reports_no_uniqueness_errors():
    from django_overlay.checks import unsupported_uniqueness
    from tests.testapp.models import SoftDeleteUniqueTest, UniqueTest

    assert unsupported_uniqueness(UniqueTest) == []
    assert unsupported_uniqueness(SoftDeleteUniqueTest) == []


# The error messages are most of this package's developer experience, so their
# wording is asserted rather than left to drift. It also makes them reachable
# for mutation testing, which is otherwise blind to a message nobody reads.


@pytest.fixture
def uniqueness_message():
    from django_overlay.checks import uniqueness_error, unsupported_uniqueness
    from tests.testapp.models import SoftDeleteUniqueTest

    model = SoftDeleteUniqueTest
    problems = [
        ("Meta.unique_together = ['first_name', 'last_name']", ("first_name", "last_name"), None),
        ("Meta.constraints has a plain UniqueConstraint 'x' (with a condition)", ("email",), "x"),
    ]
    error = uniqueness_error(model, problems)
    assert unsupported_uniqueness(model) == [], "the real model must be valid"
    return error


def test_the_uniqueness_error_names_the_model_and_lists_every_problem(uniqueness_message):
    assert uniqueness_message.msg.startswith("SoftDeleteUniqueTest declares uniqueness django_overlay can't honour:")
    assert "  - Meta.unique_together = ['first_name', 'last_name']" in uniqueness_message.msg
    assert "  - Meta.constraints has a plain UniqueConstraint 'x' (with a condition)" in uniqueness_message.msg


def test_the_uniqueness_error_explains_why(uniqueness_message):
    assert "spanning your table and the source table" in uniqueness_message.msg
    assert "uniqueness has to hold across both" in uniqueness_message.msg
    assert "a single index on your table alone" in uniqueness_message.msg
    assert "OverlayUniqueConstraint adds the source-side check." in uniqueness_message.msg


def test_the_uniqueness_hint_is_pasteable(uniqueness_message):
    assert uniqueness_message.hint.startswith("Declare them as OverlayUniqueConstraint in Meta.constraints instead:")
    assert "    constraints = [\n" in uniqueness_message.hint
    assert (
        '        OverlayUniqueConstraint(fields=["first_name", "last_name"], '
        'name="softdeleteuniquetest_first_name_last_name_uniq"),' in uniqueness_message.hint
    )
    assert '        OverlayUniqueConstraint(fields=["email"], name="x"),' in uniqueness_message.hint
    assert "any name that's unique across your models will do." in uniqueness_message.hint


def test_the_uniqueness_hint_warns_about_conditions_only_when_relevant(uniqueness_message):
    assert "Conditional uniqueness isn't supported at all" in uniqueness_message.hint
    assert "RunSQL migration and leave it out of Meta." in uniqueness_message.hint

    from django_overlay.checks import uniqueness_error
    from tests.testapp.models import SoftDeleteUniqueTest

    without = uniqueness_error(SoftDeleteUniqueTest, [("email declares unique=True", ("email",), None)])
    assert "Conditional uniqueness" not in without.hint


def test_the_uniqueness_error_carries_an_id_and_the_offending_model(uniqueness_message):
    from tests.testapp.models import SoftDeleteUniqueTest

    assert uniqueness_message.id == "django_overlay.E003"
    assert uniqueness_message.obj is SoftDeleteUniqueTest
