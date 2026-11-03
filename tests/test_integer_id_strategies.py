"""The two integer strategies that use ids exactly as stored.

NEGATIVE_ID makes the source's ids negative in the view. These two are for
data whose ids are already apart: one side stored negative, the other
positive. Nothing is negated, so a source row keeps its id in the view, the
foreign keys the source carries still point at the rows they name, and the
anti-join compares a plain column on both sides.

- NEGATIVE_SOURCE_POSITIVE_BASE_ID: the source stores negative ids; the base
  table's sequence counts up as usual.
- NEGATIVE_BASE_POSITIVE_SOURCE_ID: the source stores positive ids; the base
  table's sequence counts down from -1, and the sync keeps it that way.
"""

import pytest
from django.db import IntegrityError, connection, transaction

from django_overlay.strategies import (
    Strategy,
    default_id_field,
    default_pk_sql,
    descending_base_ids,
    negates_source_ids,
)
from django_overlay.sync import resync_view
from tests.testapp.models import (
    NegativeBasePerson,
    NegativeBasePersonNote,
    NegativeSourcePerson,
    NegativeSourcePersonNote,
)
from tests.testapp_shared.models import PersonSource


AS_STORED = (Strategy.NEGATIVE_SOURCE_POSITIVE_BASE_ID, Strategy.NEGATIVE_BASE_POSITIVE_SOURCE_ID)


def sequence_increment(model) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT seqincrement FROM pg_sequence WHERE seqrelid = %s::regclass",
            [f"{model._base_model._meta.db_table}_id_seq"],
        )
        return cursor.fetchone()[0]


def view_ids(model) -> list:
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT id FROM "{model._meta.db_table}"')  # noqa: S608 - identifier from _meta
        return [row[0] for row in cursor.fetchall()]


def refused_at_commit(note_model, **fields):
    with pytest.raises(IntegrityError), transaction.atomic():
        note_model.objects.create(**fields)
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


# ------------------------------------------------------------------ the enum


@pytest.mark.parametrize("strategy", AS_STORED)
def test_an_as_stored_strategy_negates_nothing_and_keeps_an_integer_pk(strategy):
    assert negates_source_ids(strategy) is False
    assert default_id_field(strategy) is None
    assert default_pk_sql(strategy) is None


def test_only_the_negative_base_strategy_counts_its_sequence_down():
    assert descending_base_ids(Strategy.NEGATIVE_BASE_POSITIVE_SOURCE_ID) is True
    assert descending_base_ids(Strategy.NEGATIVE_SOURCE_POSITIVE_BASE_ID) is False
    assert descending_base_ids(Strategy.NEGATIVE_ID) is False


# ------------------------------------------- negative source, positive base


@pytest.mark.django_db
def test_a_negative_source_row_keeps_its_id_in_the_view():
    PersonSource.objects.create(id=-7, first_name="Src", age=1)

    assert NegativeSourcePerson.objects.get(pk=-7).first_name == "Src"


@pytest.mark.django_db
def test_a_new_row_gets_a_positive_id_beside_negative_source_rows():
    PersonSource.objects.create(id=-7, first_name="Src", age=1)

    ours = NegativeSourcePerson.objects.create(first_name="Ours", age=2)

    assert sequence_increment(NegativeSourcePerson) > 0
    assert ours.pk > 0
    assert sorted(view_ids(NegativeSourcePerson)) == [-7, ours.pk]


@pytest.mark.django_db
def test_editing_a_negative_source_row_overrides_it_under_the_same_id():
    PersonSource.objects.create(id=-7, first_name="Src", age=1)
    person = NegativeSourcePerson.objects.get(pk=-7)

    person.age = 99
    person.save()

    assert view_ids(NegativeSourcePerson) == [-7]
    assert NegativeSourcePerson.objects.get(pk=-7).age == 99
    assert NegativeSourcePerson._base_model.objects.filter(pk=-7).exists()


@pytest.mark.django_db
def test_a_reference_to_a_negative_source_id_resolves_as_stored():
    PersonSource.objects.create(id=-7, first_name="Src", age=1)

    note = NegativeSourcePersonNote.objects.create(person_id=-7, text="hi")

    assert note.person.first_name == "Src"


@pytest.mark.django_db
def test_a_reference_to_the_negated_id_is_refused():
    """Under NEGATIVE_ID, -(-7) = 7 would be how the view names this row. Here
    it names nothing."""
    PersonSource.objects.create(id=-7, first_name="Src", age=1)

    refused_at_commit(NegativeSourcePersonNote, person_id=7, text="x")


# ------------------------------------------- negative base, positive source


@pytest.mark.django_db
def test_a_positive_source_row_keeps_its_id_in_the_view():
    source = PersonSource.objects.create(first_name="Src", age=1)

    assert source.pk > 0
    assert NegativeBasePerson.objects.get(pk=source.pk).first_name == "Src"


@pytest.mark.django_db
def test_new_rows_count_down_from_minus_one():
    first = NegativeBasePerson.objects.create(first_name="A", age=1)
    second = NegativeBasePerson.objects.create(first_name="B", age=2)

    assert sequence_increment(NegativeBasePerson) < 0
    assert first.pk < 0
    assert second.pk < first.pk


@pytest.mark.django_db
def test_editing_a_positive_source_row_overrides_it_under_the_same_id():
    source = PersonSource.objects.create(first_name="Src", age=1)
    person = NegativeBasePerson.objects.get(pk=source.pk)

    person.age = 99
    person.save()

    assert view_ids(NegativeBasePerson) == [source.pk]
    assert NegativeBasePerson.objects.get(pk=source.pk).age == 99


@pytest.mark.django_db
def test_a_reference_to_a_positive_source_id_resolves_as_stored():
    source = PersonSource.objects.create(first_name="Src", age=1)

    note = NegativeBasePersonNote.objects.create(person_id=source.pk, text="hi")

    assert note.person.first_name == "Src"
    refused_at_commit(NegativeBasePersonNote, person_id=-source.pk, text="x")


# --------------------------------------------------------- the sync's part


@pytest.mark.django_db
def test_a_resync_never_rewinds_a_sequence_already_counting_down():
    first = NegativeBasePerson.objects.create(first_name="A", age=1)

    resync_view(NegativeBasePerson)
    second = NegativeBasePerson.objects.create(first_name="B", age=2)

    assert second.pk < first.pk


@pytest.mark.django_db
def test_a_resync_turns_a_sequence_pointing_the_wrong_way_and_restarts_past_existing_rows():
    """What switching a model onto, or off, the negative base strategy does."""
    table = NegativeSourcePerson._base_model._meta.db_table
    existing = NegativeSourcePerson.objects.create(first_name="A", age=1)
    with connection.cursor() as cursor:
        cursor.execute(
            f'ALTER SEQUENCE "{table}_id_seq" INCREMENT BY -1 NO MINVALUE MAXVALUE -1 START WITH -1 RESTART WITH -1'
        )

    resync_view(NegativeSourcePerson)
    after = NegativeSourcePerson.objects.create(first_name="B", age=2)

    assert sequence_increment(NegativeSourcePerson) > 0
    assert after.pk > existing.pk


@pytest.mark.django_db
def test_turning_a_sequence_down_restarts_below_existing_negative_rows():
    table = NegativeBasePerson._base_model._meta.db_table
    lowest = NegativeBasePerson.objects.create(first_name="A", age=1)
    with connection.cursor() as cursor:
        cursor.execute(
            f'ALTER SEQUENCE "{table}_id_seq" INCREMENT BY 1 MINVALUE 1 NO MAXVALUE START WITH 1 RESTART WITH 1'
        )

    resync_view(NegativeBasePerson)
    after = NegativeBasePerson.objects.create(first_name="B", age=2)

    assert sequence_increment(NegativeBasePerson) < 0
    assert after.pk < lowest.pk
