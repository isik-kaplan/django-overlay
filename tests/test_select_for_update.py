"""`FOR UPDATE` through the view locks nothing, so select_for_update() is refused.

Postgres accepts `SELECT ... FOR UPDATE` against a view with INSTEAD OF
triggers and then declines to mark any rows: it takes a table-level
RowShareLock and stops there. A second transaction can UPDATE the same row
immediately. The tests below hold a lock on one connection and race an UPDATE
on another to show the difference against the base table, which does block.

That makes select_for_update() worse than unsupported — it would look like
mutual exclusion and provide none — so OverlayQuerySet raises instead.
"""

import threading
import time

import psycopg
import pytest
from django.conf import settings
from django.db import connection, models, transaction

from django_overlay.exceptions import OverlayConfigurationError
from tests.testapp.models import Person, UniqueTestNoSource
from tests.testapp_shared.models import PersonSource


pytestmark = pytest.mark.django_db(transaction=True)


def raw_connection():
    db = settings.DATABASES["default"]
    return psycopg.connect(
        host=db["HOST"],
        port=db["PORT"],
        dbname=db["NAME"],
        user=db["USER"],
        password=db["PASSWORD"] or None,
        autocommit=False,
    )


def blocks(holder_sql, contender_sql, pk, timeout=10.0):
    """Hold `holder_sql`'s lock on one connection, run `contender_sql` on
    another, and report whether the second had to wait.

    Asks Postgres who is blocking whom rather than timing it. A fixed sleep
    here was flaky: under load the contender could still be starting when the
    clock ran out, and a slow machine would report a block that wasn't one."""
    finished = threading.Event()
    started = threading.Event()
    failures = []
    contender_pid = []

    holder = raw_connection()
    with holder.cursor() as cursor:
        cursor.execute(holder_sql, (pk,))
        cursor.fetchall()

    def contend():
        conn = raw_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                contender_pid.append(cursor.fetchone()[0])
                started.set()
                cursor.execute(contender_sql, (pk,))
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            finished.set()
            conn.close()

    thread = threading.Thread(target=contend)
    thread.start()
    started.wait(timeout=timeout)

    blocked = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if finished.is_set():
            break
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_blocking_pids(%s)", [contender_pid[0]])
            if cursor.fetchone()[0]:
                blocked = True
                break
        time.sleep(0.02)

    holder.rollback()
    thread.join(timeout=timeout)
    holder.close()
    assert not failures, failures
    return blocked


@pytest.fixture
def person():
    Person.objects.all().delete()
    PersonSource.objects.all().delete()
    yield Person.objects.create(first_name="x", age=1)
    Person.objects.all().delete()
    PersonSource.objects.all().delete()


@pytest.mark.concurrency
def test_locking_the_base_table_blocks_a_concurrent_update(person):
    """The control: row locking works fine on an ordinary table."""
    assert blocks(
        "SELECT id FROM person WHERE id = %s FOR UPDATE",
        "UPDATE person SET age = age + 1 WHERE id = %s",
        person.pk,
    )


@pytest.mark.concurrency
def test_locking_through_the_view_blocks_nothing(person):
    """The finding. Same row, same contender, no protection."""
    assert not blocks(
        "SELECT id FROM person_view WHERE id = %s FOR UPDATE",
        "UPDATE person SET age = age + 1 WHERE id = %s",
        person.pk,
    )


@pytest.mark.concurrency
def test_locking_through_the_view_does_not_even_block_another_locker(person):
    assert not blocks(
        "SELECT id FROM person_view WHERE id = %s FOR UPDATE",
        "SELECT id FROM person_view WHERE id = %s FOR UPDATE",
        person.pk,
    )


def test_select_for_update_is_refused_rather_than_silently_useless():
    with pytest.raises(OverlayConfigurationError, match="locking any rows"):
        list(Person.objects.select_for_update().filter(first_name="x"))


def test_the_refusal_points_at_something_that_works():
    with pytest.raises(OverlayConfigurationError) as exc_info:
        Person.objects.select_for_update()

    message = str(exc_info.value)
    assert "update(field=F('field') + 1)" in message
    assert "pg_advisory_xact_lock" in message


def test_it_is_refused_on_a_source_less_overlay_model_too():
    # Nothing to do with the source: the view and its triggers are the reason.
    with pytest.raises(OverlayConfigurationError):
        UniqueTestNoSource.objects.select_for_update()


def test_an_f_expression_is_the_answer_for_a_read_modify_write(person):
    """What select_for_update() would have been used for. Atomic since
    OverlayQuerySet.update() started routing these around the view — the
    contention case is covered in tests/test_atomic_update.py."""
    Person.objects.filter(pk=person.pk).update(age=models.F("age") + 1)

    assert Person.objects.get(pk=person.pk).age == 2


@pytest.mark.concurrency
def test_an_advisory_lock_does_serialise_a_critical_section(person):
    """The other documented alternative, end to end."""
    order = []

    def critical(label):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [42, person.pk])
            order.append(f"{label}-in")
            time.sleep(0.2)
            order.append(f"{label}-out")
        connection.close()

    threads = [threading.Thread(target=critical, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
        time.sleep(0.05)
    for thread in threads:
        thread.join(timeout=30)

    # Interleaved would be a-in, b-in, a-out, b-out.
    assert order in (["a-in", "a-out", "b-in", "b-out"], ["b-in", "b-out", "a-in", "a-out"]), order


def test_update_or_create_still_works(person):
    """Django's update_or_create() locks the row it found with
    select_for_update(). Refusing that would break a core ORM method over a
    lock that does nothing here anyway, so Django's own use is let through."""
    obj, created = Person.objects.update_or_create(pk=person.pk, defaults={"age": 99})

    assert not created
    assert Person.objects.get(pk=person.pk).age == 99


def test_update_or_create_creates_when_missing():
    obj, created = Person.objects.update_or_create(first_name="brand-new", defaults={"age": 7})

    assert created
    assert obj.age == 7
    Person.objects.all().delete()


def test_the_internal_allowance_does_not_leak(person):
    """The flag is scoped to the update_or_create call, so a direct call after
    one still raises."""
    Person.objects.update_or_create(pk=person.pk, defaults={"age": 3})

    with pytest.raises(OverlayConfigurationError):
        Person.objects.select_for_update()
