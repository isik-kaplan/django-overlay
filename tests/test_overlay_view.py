import pytest
from django.db import IntegrityError, transaction

from tests.testapp.registry import STRATEGIES


pytestmark = pytest.mark.django_db


def negates(strategy_name):
    return strategy_name == "negative_id"


def exposed_id(strategy_name, raw_id):
    return -raw_id if negates(strategy_name) else raw_id


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_untouched_source_row_appears_through_the_view(strategy_name, db_cursor):
    m = STRATEGIES[strategy_name]
    source = m["PersonSource"].objects.create(first_name="Source Jane", age=40)

    person = m["Person"].objects.get(id=exposed_id(strategy_name, source.id))

    assert person.first_name == "Source Jane"
    db_cursor.execute(f"SELECT count(*) FROM {m['Person'].base_table()._meta.db_table} WHERE id = %s", [source.id])
    assert db_cursor.fetchone()[0] == 0


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_organic_create_does_not_collide_with_an_untouched_source_row(strategy_name, db_cursor):
    m = STRATEGIES[strategy_name]
    source = m["PersonSource"].objects.create(first_name="Source Someone", age=99)

    person = m["Person"].objects.create(first_name="Organic Alice", age=30)

    assert person.id != source.id
    assert m["Person"].objects.get(id=exposed_id(strategy_name, source.id)).first_name == "Source Someone"
    assert m["Person"].objects.get(id=person.id).first_name == "Organic Alice"


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_update_on_an_untouched_source_row_materializes_it(strategy_name, db_cursor):
    m = STRATEGIES[strategy_name]
    source = m["PersonSource"].objects.create(first_name="Source Bob", age=55)
    view_id = exposed_id(strategy_name, source.id)

    m["Person"].objects.filter(id=view_id).update(age=56)

    base_table = m["Person"].base_table()._meta.db_table
    db_cursor.execute(f"SELECT first_name, age FROM {base_table} WHERE id = %s", [view_id])
    assert db_cursor.fetchone() == ("Source Bob", 56)
    assert m["Person"].objects.get(id=view_id).age == 56


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_second_update_does_not_duplicate_the_materialized_row(strategy_name, db_cursor):
    m = STRATEGIES[strategy_name]
    source = m["PersonSource"].objects.create(first_name="Source Carl", age=60)
    view_id = exposed_id(strategy_name, source.id)
    m["Person"].objects.filter(id=view_id).update(age=61)

    m["Person"].objects.filter(id=view_id).update(age=62)

    base_table = m["Person"].base_table()._meta.db_table
    db_cursor.execute(f"SELECT count(*) FROM {base_table} WHERE id = %s", [view_id])
    assert db_cursor.fetchone()[0] == 1
    assert m["Person"].objects.get(id=view_id).age == 62


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_delete_removes_the_base_row(strategy_name, db_cursor):
    m = STRATEGIES[strategy_name]
    person = m["Person"].objects.create(first_name="Temporary", age=1)

    person.delete()

    base_table = m["Person"].base_table()._meta.db_table
    db_cursor.execute(f"SELECT count(*) FROM {base_table} WHERE id = %s", [person.id])
    assert db_cursor.fetchone()[0] == 0
    assert not m["Person"].objects.filter(id=person.id).exists()


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_one_to_one_bonus_table_accepts_a_reference_to_an_overlay_model(strategy_name):
    m = STRATEGIES[strategy_name]
    person = m["Person"].objects.create(first_name="Has Profile", age=20)

    profile = m["PersonProfile"].objects.create(person=person, bio="hello")

    assert profile.person_id == person.id
    assert person.profile.bio == "hello"


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_foreign_key_bonus_table_accepts_a_reference_to_an_overlay_model(strategy_name):
    m = STRATEGIES[strategy_name]
    address = m["Address"].objects.create(street="1 Main St", city="Springfield")

    note = m["AddressNote"].objects.create(address=address, text="ok")

    assert note.pk is not None
    assert list(address.notes.all()) == [note]


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_foreign_key_bonus_table_rejects_a_reference_to_a_nonexistent_id(strategy_name, db_cursor):
    m = STRATEGIES[strategy_name]
    # Deferred constraint: only checked at real commit, so force it rather
    # than rely on this atomic() block's exit (a savepoint under pytest-django).
    bogus_address_id = -999999 if negates(strategy_name) else "00000000-0000-0000-0000-000000000000"
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            m["AddressNote"].objects.create(address_id=bogus_address_id, text="should fail")
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
