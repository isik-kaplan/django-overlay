"""A project that migrated an overlay model with inherited fields under 0.2.0,
then upgraded.

Migration 0036 is what 0.2.0 generated for InheritedFields: its base table
kept the inherited one-to-one's UNIQUE and got an injected primary key with no
comment. 0037 is what makemigrations writes after the upgrade, and has to apply
on a table the overlay view selects from."""

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction

from tests.testapp.models import InheritedFields, Vendor


pytestmark = pytest.mark.django_db(transaction=True)


def _unique_constraints():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'inheritedfields'::regclass AND contype = 'u'"
        )
        return [row[0] for row in cursor.fetchall()]


def _comments():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT a.attname, col_description(a.attrelid, a.attnum) FROM pg_attribute a "
            "WHERE a.attrelid = 'inheritedfields'::regclass AND a.attname IN ('id', '_overlay_deleted', 'created_at')"
        )
        return dict(cursor.fetchall())


def test_the_upgrade_drops_the_table_level_unique_and_adds_the_comments():
    try:
        call_command("migrate", "testapp", "0036", verbosity=0)
        assert len(_unique_constraints()) == 1
        assert _comments() == {"id": None, "_overlay_deleted": None, "created_at": "When it was made."}
    finally:
        call_command("migrate", "testapp", verbosity=0)

    assert _unique_constraints() == []
    comments = _comments()
    assert comments["id"] == "Row id."
    assert comments["created_at"] == "When it was made."
    assert comments["_overlay_deleted"].startswith("Set by django-overlay")


def _refuses_a_second_row_for_one_vendor():
    vendor = Vendor.objects.create(name=f"v{Vendor.objects.count()}")
    InheritedFields.objects.create(vendor=vendor)
    InheritedFields.objects.create()
    InheritedFields.objects.create()

    with pytest.raises(IntegrityError), transaction.atomic():
        InheritedFields.objects.create(vendor=vendor)

    assert InheritedFields.objects.get(vendor=vendor).vendor == vendor
    assert vendor.inheritedfields.vendor_id == vendor.pk


def test_after_the_upgrade_the_one_to_one_is_still_unique_through_the_view():
    _refuses_a_second_row_for_one_vendor()


def test_rolling_the_upgrade_back_and_forward_keeps_the_one_to_one_unique():
    """Reversing 0037 turns the base table's ForeignKey back into a
    OneToOneField, and Django drops every plain btree index on the column
    while doing it, the partial unique index the soft-delete narrowing made
    included. AlterOverlayField puts it back."""
    try:
        call_command("migrate", "testapp", "0036", verbosity=0)
        assert "inheritedfields_vendor_uniq" in _indexes()
    finally:
        call_command("migrate", "testapp", verbosity=0)

    assert "inheritedfields_vendor_uniq" in _indexes()
    _refuses_a_second_row_for_one_vendor()


def _indexes():
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'inheritedfields'")
        return {row[0] for row in cursor.fetchall()}
