"""`update()` with an expression that reads the row it's updating.

On a real table `update(age=F("age") + 1)` is atomic: Postgres takes the row
lock, re-reads, and re-evaluates. Through the view there is nothing left to
re-evaluate — the expression is folded into a literal before the INSTEAD OF
trigger sees it — so concurrent increments overwrite each other.

OverlayQuerySet.update() spots that shape and routes around the view: copy the
matched rows into the base table, then update that table directly, where the
ordinary locking semantics apply. Everything else keeps the single-statement
path.
"""

import threading

import pytest
from django.db import connection, models
from django.test.utils import CaptureQueriesContext

from django_overlay.models import _reads_own_columns
from tests.testapp.models import Person, SoftDeleteTest
from tests.testapp_shared.models import PersonSource, SoftDeleteTestSource


pytestmark = pytest.mark.django_db


# ------------------------------------------------------- which path is taken


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (5, False),
        ("a string", False),
        (models.Value(3), False),
        (models.F("age"), True),
        (models.F("age") + 1, True),
        (models.F("first_name"), True),
        (2 * models.F("age"), True),
        (models.Case(models.When(age=1, then=models.F("age") + 1), default=0), True),
        (models.Func(models.F("age"), function="ABS"), True),
    ],
)
def test_detecting_an_expression_that_reads_its_own_row(expression, expected):
    assert _reads_own_columns(expression, Person) is expected


def test_a_literal_update_keeps_the_single_statement_path():
    person = Person.objects.create(first_name="x", age=1)

    with CaptureQueriesContext(connection) as queries:
        Person.objects.filter(pk=person.pk).update(age=5)

    statements = [q["sql"] for q in queries if "person" in q["sql"].lower()]
    assert len(statements) == 1
    assert "person_view" in statements[0], "literals still go through the view"


def test_a_self_referencing_update_takes_the_base_table_path():
    person = Person.objects.create(first_name="x", age=1)

    with CaptureQueriesContext(connection) as queries:
        Person.objects.filter(pk=person.pk).update(age=models.F("age") + 1)

    sql = " ".join(q["sql"] for q in queries)
    assert "INSERT INTO" in sql and "ON CONFLICT" in sql, "matched rows are materialised first"
    assert 'UPDATE "person" SET' in sql, "then updated on the base table"


# ------------------------------------------------------------ correctness


def test_an_f_expression_updates_an_organic_row():
    person = Person.objects.create(first_name="x", age=1)

    assert Person.objects.filter(pk=person.pk).update(age=models.F("age") + 1) == 1
    assert Person.objects.get(pk=person.pk).age == 2


def test_an_f_expression_materialises_and_updates_a_source_only_row(db_cursor):
    source = PersonSource.objects.create(first_name="Src", age=10)

    updated = Person.objects.filter(pk=-source.id).update(age=models.F("age") + 5)

    assert updated == 1
    assert Person.objects.get(pk=-source.id).age == 15
    assert Person.objects.get(pk=-source.id).first_name == "Src", "the other columns come across too"
    db_cursor.execute("SELECT age FROM testapp_shared_personsource WHERE id = %s", [source.id])
    assert db_cursor.fetchone() == (10,), "the source table is read-only and stays pristine"


def test_an_f_expression_across_a_mix_of_organic_and_source_rows():
    source = PersonSource.objects.create(first_name="s", age=1)
    organic = Person.objects.create(first_name="o", age=1)

    updated = Person.objects.filter(pk__in=[-source.id, organic.pk]).update(age=models.F("age") + 1)

    assert updated == 2
    assert sorted(Person.objects.values_list("age", flat=True)) == [2, 2]


def test_an_f_expression_on_a_soft_delete_model():
    """The base table has a column the view doesn't, so materialising has to
    supply it."""
    source = SoftDeleteTestSource.objects.create(first_name="src")
    SoftDeleteTest.objects.create(first_name="organic")

    updated = SoftDeleteTest.objects.all().update(first_name=models.F("first_name"))

    assert updated == 2
    assert SoftDeleteTest.objects.filter(pk=-source.id).exists(), "still visible, not tombstoned"


def test_a_soft_deleted_row_is_not_resurrected_by_an_update():
    target = SoftDeleteTest.objects.create(first_name="gone")
    pk = target.pk
    target.delete()

    SoftDeleteTest.objects.all().update(first_name=models.F("first_name"))

    assert not SoftDeleteTest.objects.filter(pk=pk).exists()


def test_mixing_a_literal_and_an_expression_in_one_update():
    person = Person.objects.create(first_name="x", age=1)

    Person.objects.filter(pk=person.pk).update(age=models.F("age") + 1, first_name="renamed")

    fresh = Person.objects.get(pk=person.pk)
    assert (fresh.age, fresh.first_name) == (2, "renamed")


def test_an_update_matching_nothing_reports_zero():
    assert Person.objects.filter(first_name="absent").update(age=models.F("age") + 1) == 0


def test_a_second_self_referencing_update_does_not_duplicate_the_row(db_cursor):
    source = PersonSource.objects.create(first_name="s", age=0)

    Person.objects.filter(pk=-source.id).update(age=models.F("age") + 1)
    Person.objects.filter(pk=-source.id).update(age=models.F("age") + 1)

    db_cursor.execute("SELECT count(*) FROM person WHERE id = %s", [-source.id])
    assert db_cursor.fetchone() == (1,)
    assert Person.objects.get(pk=-source.id).age == 2


def test_copying_one_column_into_another():
    person = Person.objects.create(first_name="x", age=7)

    Person.objects.filter(pk=person.pk).update(first_name=models.F("age"))

    assert Person.objects.get(pk=person.pk).first_name == "7"


# ------------------------------------------------------------ concurrency


@pytest.mark.concurrency
@pytest.mark.django_db(transaction=True)
def test_concurrent_increments_do_not_lose_each_other():
    """The whole point. Through the view this lands around 60% of the time."""
    Person.objects.all().delete()
    person = Person.objects.create(first_name="counter", age=0)
    threads_count, per_thread = 4, 40

    def bump():
        for _ in range(per_thread):
            Person.objects.filter(pk=person.pk).update(age=models.F("age") + 1)
        connection.close()

    threads = [threading.Thread(target=bump) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert Person.objects.get(pk=person.pk).age == threads_count * per_thread
    Person.objects.all().delete()


@pytest.mark.concurrency
@pytest.mark.django_db(transaction=True)
def test_concurrent_increments_of_a_source_backed_row():
    """Same, starting from a row that has to be materialised first — so the
    materialisation itself has to tolerate losing the race."""
    Person.objects.all().delete()
    PersonSource.objects.all().delete()
    source = PersonSource.objects.create(first_name="counter", age=0)
    threads_count, per_thread = 4, 40

    def bump():
        for _ in range(per_thread):
            Person.objects.filter(pk=-source.id).update(age=models.F("age") + 1)
        connection.close()

    threads = [threading.Thread(target=bump) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert Person.objects.get(pk=-source.id).age == threads_count * per_thread
    Person.objects.all().delete()
    PersonSource.objects.all().delete()


def test_an_f_expression_on_a_model_that_opts_out_of_soft_delete():
    """No `_overlay_deleted` column to supply when materialising."""
    from tests.testapp.models import UniqueTestNoSource

    row = UniqueTestNoSource.objects.create(ssn="abc")

    updated = UniqueTestNoSource.objects.filter(pk=row.pk).update(ssn=models.F("ssn"))

    assert updated == 1
    assert UniqueTestNoSource.objects.get(pk=row.pk).ssn == "abc"


# `update()` is not the only way an F() reaches the database. save() calls the
# private _update(), and it asks for the values back so it can put the resolved
# number on the instance in place of the expression. bulk_update() needs
# nothing extra — it builds a Case/When and hands it to update().


def test_save_with_an_expression_takes_the_base_table_path():
    person = Person.objects.create(first_name="x", age=1)
    stale = Person(pk=person.pk)
    stale.age = models.F("age") + 1

    with CaptureQueriesContext(connection) as queries:
        stale.save(update_fields=["age"])

    sql = " ".join(q["sql"] for q in queries)
    assert "INSERT INTO" in sql and "ON CONFLICT" in sql, "matched rows are materialised first"
    assert 'UPDATE "person" SET' in sql, "and the write lands on the base table"
    assert 'UPDATE "person_view" SET' not in sql, "never on the view, which folds the expression"
    assert Person.objects.get(pk=person.pk).age == 2


def test_save_with_an_expression_resolves_it_on_the_instance():
    """What returning_fields is for: after save() the attribute has to be the
    number, not the expression, or the next save would apply it twice."""
    person = Person.objects.create(first_name="x", age=1)
    person.age = models.F("age") + 1

    person.save(update_fields=["age"])

    assert person.age == 2, "the resolved value, read back after the routed update"
    person.save(update_fields=["age"])
    assert Person.objects.get(pk=person.pk).age == 2, "and saving again does not re-apply it"


def test_save_with_an_expression_on_a_source_only_row():
    source = PersonSource.objects.create(first_name="Src", age=10)
    row = Person.objects.get(pk=-source.id)
    row.age = models.F("age") + 5

    row.save(update_fields=["age"])

    assert row.age == 15
    assert Person.objects.get(pk=-source.id).age == 15


def test_save_with_a_literal_keeps_the_view_path():
    person = Person.objects.create(first_name="x", age=1)
    person.age = 9

    with CaptureQueriesContext(connection) as queries:
        person.save(update_fields=["age"])

    assert any("person_view" in q["sql"] for q in queries)


def test_saving_a_row_that_no_longer_exists_raises_like_django_does():
    """The zero-rows branch. Django turns "matched nothing" into NotUpdated
    when update_fields is given, and it has to keep doing that on the routed
    path — silently succeeding would be worse than the bug this fixes."""
    person = Person.objects.create(first_name="x", age=1)
    pk = person.pk
    Person.objects.filter(pk=pk).delete()

    ghost = Person(pk=pk)
    ghost.age = models.F("age") + 1

    # save_base wraps the write in atomic(savepoint=False), so the raise leaves
    # the surrounding transaction unusable — same as on a real table, which is
    # why the row is checked before rather than after.
    assert not Person.objects.filter(pk=pk).exists()

    with pytest.raises(Person.NotUpdated):
        ghost.save(update_fields=["age"])


def test_bulk_update_with_an_expression_goes_through_update():
    person = Person.objects.create(first_name="x", age=1)
    person.age = models.F("age") + 1

    Person.objects.bulk_update([person], ["age"])

    assert Person.objects.get(pk=person.pk).age == 2


def test_the_count_returning_form_of_the_private_update():
    """save() always asks for the values back, but `_update(values)` returning
    a count is part of the QuerySet contract, so the routed path honours it."""
    person = Person.objects.create(first_name="x", age=1)
    field = Person._meta.get_field("age")

    updated = Person.objects.filter(pk=person.pk)._update([(field, None, models.F("age") + 1)])

    assert updated == 1
    assert Person.objects.get(pk=person.pk).age == 2
