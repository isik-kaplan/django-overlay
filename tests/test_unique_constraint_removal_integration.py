import pytest

from tests.testapp.models import RemovableUniqueTest
from tests.testapp_shared.models import RemovableUniqueTestSource


pytestmark = pytest.mark.django_db


def test_a_removed_overlay_unique_constraint_no_longer_rejects_a_collision_with_source():
    RemovableUniqueTestSource.objects.create(ssn="555-55-5555")

    # The constraint (and its trigger) was dropped in migration 0013.
    RemovableUniqueTest.objects.create(ssn="555-55-5555")


def test_a_removed_overlay_unique_constraint_no_longer_rejects_an_organic_collision():
    # Django's RemoveConstraint dropped the native UNIQUE too, so even a
    # plain base-vs-base duplicate is allowed now.
    RemovableUniqueTest.objects.create(ssn="666-66-6666")
    RemovableUniqueTest.objects.create(ssn="666-66-6666")


def test_the_orphaned_unique_constraint_trigger_is_actually_gone(db_cursor):
    db_cursor.execute("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'overlayunique_removableuniquetest%%'")
    assert db_cursor.fetchone() is None
