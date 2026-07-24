import pytest

from tests.testapp.models import RemovableFkTest


pytestmark = pytest.mark.django_db


def test_writing_to_a_table_whose_overlay_fk_was_removed_does_not_error(db_cursor):
    obj = RemovableFkTest.objects.create(label="hello")
    assert RemovableFkTest.objects.get(pk=obj.pk).label == "hello"


def test_the_orphaned_constraint_trigger_is_actually_gone(db_cursor):
    db_cursor.execute("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'overlayfk_testapp_removablefktest%%'")
    assert db_cursor.fetchone() is None


def test_the_address_column_is_actually_gone(db_cursor):
    db_cursor.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'testapp_removablefktest' AND column_name = 'address_id'"
    )
    assert db_cursor.fetchone() is None
