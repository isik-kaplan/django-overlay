import pytest
from django.core.management import call_command
from django.db import connection

from tests.testapp.models import RemovableFkTest, ReservedWord, UniqueTest


pytestmark = pytest.mark.django_db(transaction=True)


def _view_exists(view_name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s)", [f"public.{view_name}"])
        return cursor.fetchone()[0] is not None


def test_testapp_migrations_reverse_all_the_way_and_reapply_cleanly():
    # Reapply to latest even if an assertion fails, so this doesn't corrupt
    # the DB for every other test in the session.
    try:
        call_command("migrate", "testapp", "0001", verbosity=0)

        assert not _view_exists("reservedword_view")
        assert not _view_exists("uniquetest_view")
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('testapp_removablefktest')")
            assert cursor.fetchone()[0] is None
    finally:
        call_command("migrate", "testapp", verbosity=0)

    obj = ReservedWord.objects.create(order=7)
    assert ReservedWord.objects.get(pk=obj.pk).order == 7
    assert UniqueTest.objects.create(ssn="000-11-2222").ssn == "000-11-2222"
    assert RemovableFkTest.objects.create(label="back").label == "back"


def test_reversing_a_field_removal_does_not_restore_its_trigger():
    # RemoveOverlayConstraint.backward() is a no-op (see its docstring), so
    # the column comes back but the trigger doesn't.
    try:
        call_command("migrate", "testapp", "0010_removablefktest", verbosity=0)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'testapp_removablefktest' AND column_name = 'address_id'"
            )
            assert cursor.fetchone() is not None

            cursor.execute("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'overlayfk_testapp_removablefktest%%'")
            assert cursor.fetchone() is None
    finally:
        call_command("migrate", "testapp", verbosity=0)

    assert RemovableFkTest.objects.create(label="forward-again").label == "forward-again"
