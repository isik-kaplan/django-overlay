"""When a violation surfaces, measured against Django's own constraints.

"Deferred to COMMIT" is only a divergence if Django is immediate. On
PostgreSQL Django emits foreign keys as DEFERRABLE INITIALLY DEFERRED, so an
FK violation lands on COMMIT in a plain Django project too — django_overlay's
FK trigger matches that exactly and deliberately.

Uniqueness is the other way round: Django's is a plain unique index, which
raises at the statement, so the source-side trigger is INITIALLY IMMEDIATE to
match. Deferring only that half would mean one declared constraint failing at
two different moments depending on which side the collision was on.

These run with transaction=True because the question is literally "statement or
COMMIT", and under a rolled-back test there is no COMMIT to observe.
"""

import pytest
from django.db import IntegrityError, connection, transaction

from tests.testapp.models import (
    AddressNote,
    PersonNote,
    PhoneTag,
    PhoneTagPhoneThrough,
    UniqueTest,
    UniqueTestNoSource,
)
from tests.testapp_shared.models import UniqueTestSource


pytestmark = pytest.mark.django_db(transaction=True)


def when_does_it_raise(write) -> str:
    """'statement', 'COMMIT', or 'never'."""
    try:
        with transaction.atomic():
            try:
                write()
            except IntegrityError:
                return "statement"
        return "never"
    except IntegrityError:
        return "COMMIT"


@pytest.fixture(autouse=True)
def _clean():
    yield
    for model in (PhoneTagPhoneThrough, PhoneTag, AddressNote, PersonNote, UniqueTest, UniqueTestNoSource):
        model.objects.all().delete()
    UniqueTestSource.objects.all().delete()


def test_djangos_own_foreign_key_defers_to_commit_on_postgres():
    """The baseline everything else is measured against. If this ever reports
    'statement', Django changed and OverlayForeignKey should follow."""
    assert when_does_it_raise(lambda: PhoneTagPhoneThrough.objects.create(phonetag_id=999999, phone_id=1)) == "COMMIT"


def test_overlay_foreign_key_matches_djangos_timing():
    assert when_does_it_raise(lambda: AddressNote.objects.create(address_id=-999999, text="x")) == "COMMIT"


def test_an_overlay_to_overlay_foreign_key_matches_it_too():
    assert when_does_it_raise(lambda: PersonNote.objects.create(person_id=-999999, text="x")) == "COMMIT"


def test_django_emits_its_foreign_keys_as_deferred(db_cursor):
    db_cursor.execute(
        "SELECT condeferrable, condeferred FROM pg_constraint "
        "WHERE contype = 'f' AND conrelid = 'testapp_phonetagphonethrough'::regclass"
    )

    assert all(deferrable and deferred for deferrable, deferred in db_cursor.fetchall())


def test_a_native_unique_index_raises_at_the_statement():
    UniqueTestNoSource.objects.create(ssn="dup")

    assert when_does_it_raise(lambda: UniqueTestNoSource.objects.create(ssn="dup")) == "statement"


def test_a_local_overlay_unique_collision_matches_the_native_index():
    UniqueTest.objects.create(ssn="dup")

    assert when_does_it_raise(lambda: UniqueTest.objects.create(ssn="dup")) == "statement"


def test_a_source_side_overlay_unique_collision_matches_it_too():
    """The half that used to be deferred, so one constraint failed at two
    different moments depending on which side you collided with."""
    UniqueTestSource.objects.create(ssn="dup")

    assert when_does_it_raise(lambda: UniqueTest.objects.create(ssn="dup")) == "statement"


def test_the_unique_trigger_is_still_deferrable_on_request():
    """INITIALLY IMMEDIATE, not NOT DEFERRABLE: code that wants the old
    behaviour can still ask for it per transaction."""
    UniqueTestSource.objects.create(ssn="dup")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL DEFERRED")
            UniqueTest.objects.create(ssn="dup")  # accepted here...
    # ...and rejected at COMMIT, which is where the atomic block raised.


def test_the_fk_trigger_can_be_made_immediate_on_request():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            AddressNote.objects.create(address_id=-999999, text="x")
