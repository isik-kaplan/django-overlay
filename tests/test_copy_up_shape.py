"""What a copy-up leaves behind, as anything watching the base table sees it.

The INSTEAD OF UPDATE trigger materialises a source-backed row and applies the
edit in two statements rather than one. The final row is the same either way --
tests/test_query_shapes.py and tests/test_atomic_update.py already pin that
across every ORM shape and both id strategies -- so equivalence is not what
these tests are for.

What they pin is the *shape of the writes*, which is the whole reason for the
second statement. A row-level AFTER trigger on the base table -- django-pghistory's,
or a hand-rolled audit table like the one below -- sees a materialise carrying
the source's own values, then a change touching only what the caller edited.
Collapsed into one INSERT, the first edit of a row reports every column as
written, and "has this tenant overridden this field" becomes unanswerable.
"""

import pytest
from django.db import connection

from tests.testapp.models import Person
from tests.testapp_shared.models import PersonSource


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def base_writes():
    """Every row-level write to person, in order, as (op, first_name, age)."""
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TEMP TABLE overlay_audit (
                seq serial PRIMARY KEY, op text, first_name text, age integer
            );
            CREATE OR REPLACE FUNCTION overlay_audit_fn() RETURNS TRIGGER AS $$
            BEGIN
              INSERT INTO overlay_audit (op, first_name, age)
              VALUES (TG_OP, NEW.first_name, NEW.age);
              RETURN NEW;
            END; $$ LANGUAGE plpgsql;
            CREATE TRIGGER overlay_audit_trigger AFTER INSERT OR UPDATE ON person
            FOR EACH ROW EXECUTE FUNCTION overlay_audit_fn();
        """)

        def read():
            cursor.execute("SELECT op, first_name, age FROM overlay_audit ORDER BY seq")
            return cursor.fetchall()

        try:
            yield read
        finally:
            cursor.execute("DROP TRIGGER IF EXISTS overlay_audit_trigger ON person")
            cursor.execute("DROP TABLE IF EXISTS overlay_audit")


def test_a_first_edit_of_a_source_row_materialises_then_changes(base_writes):
    """Two writes: the source's values, then only what the caller asked for."""
    source = PersonSource.objects.create(first_name="Src", age=10)

    Person.objects.filter(pk=-source.id).update(age=11)

    assert base_writes() == [
        ("INSERT", "Src", 10),
        ("UPDATE", "Src", 11),
    ], "the materialise must carry the source's own values, not the edited ones"


def test_the_materialise_is_distinguishable_from_the_edit(base_writes):
    """The point of the split: diffing the two names the overridden column.

    This is what a refresh-from-source has to ask before overwriting anything,
    and what one combined INSERT cannot answer -- there, every column of the
    first edit looks written.
    """
    source = PersonSource.objects.create(first_name="Src", age=10)

    Person.objects.filter(pk=-source.id).update(age=11)

    materialised, edited = base_writes()
    changed = [
        name
        for name, before, after in zip(("first_name", "age"), materialised[1:], edited[1:], strict=True)
        if before != after
    ]
    assert changed == ["age"], "only the column the caller edited may differ between the two"


def test_a_second_edit_writes_once(base_writes):
    """Only the first edit pays for the split; the row already exists after it."""
    source = PersonSource.objects.create(first_name="Src", age=10)
    Person.objects.filter(pk=-source.id).update(age=11)

    Person.objects.filter(pk=-source.id).update(age=12)

    assert [row[0] for row in base_writes()] == ["INSERT", "UPDATE", "UPDATE"]


def test_an_organic_row_is_not_split(base_writes):
    """Nothing to materialise -- an organic row is born in the base table."""
    person = Person.objects.create(first_name="Org", age=1)

    Person.objects.filter(pk=person.pk).update(age=2)

    assert [row[0] for row in base_writes()] == ["INSERT", "UPDATE"]
