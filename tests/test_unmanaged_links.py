"""An OverlayForeignKey on an unmanaged model: the table is someone else's, so nothing is put on it."""

import pytest
from django.db import connection

from django_overlay.sync import inbound_overlay_foreign_keys, resync_view
from tests.testapp.models import Person, UnmanagedPersonLink


pytestmark = pytest.mark.django_db


def test_it_is_not_counted_among_the_links_into_its_target():
    assert UnmanagedPersonLink not in [model for model, _field in inbound_overlay_foreign_keys(Person)]


def test_resyncing_its_target_leaves_it_alone():
    """Its table does not exist, so a trigger aimed at it would fail the resync."""
    resync_view(Person)

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_trigger WHERE tgname LIKE %s", ["%person_links%"])
        assert cursor.fetchone()[0] == 0
