import pytest
from django.db import IntegrityError, transaction

from tests.testapp.models import Person, RenameFieldTest, RenameFkTest


pytestmark = pytest.mark.django_db


def test_writes_through_a_renamed_field_work(db_cursor):
    obj = RenameFieldTest.objects.create(renamed_field="hello")

    RenameFieldTest.objects.filter(id=obj.id).update(renamed_field="updated")

    assert RenameFieldTest.objects.get(id=obj.id).renamed_field == "updated"
    db_cursor.execute("SELECT renamed_field FROM renamefieldtest WHERE id = %s", [obj.id])
    assert db_cursor.fetchone()[0] == "updated"


def test_writes_through_a_renamed_foreign_key_column_work():
    real_person = Person.objects.create(first_name="Jane", age=30)
    link = RenameFkTest.objects.create(renamed_fk=real_person)
    assert link.renamed_fk_id == real_person.id
    assert list(real_person.rename_fk_tests.all()) == [link]


def test_the_renamed_foreign_keys_constraint_still_rejects_a_nonexistent_id(db_cursor):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            RenameFkTest.objects.create(renamed_fk_id=-999999)
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
