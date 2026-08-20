"""The ORM surface, pinned.

`tests/probe_orm_conformance.py` sweeps ~48 behaviours and reports which match
a plain Django model. A probe reports; it doesn't protect. These are the ones
worth protecting — the paths where the view or the INSTEAD OF triggers do real
work, so a regression would be silent rather than obvious.

Each is exercised against both an organic row (base table only) and a
source-backed one (copy-on-write through the update trigger), because those
take different paths.
"""

import pytest
from django.core import serializers
from django.db import connection, models
from django.db.models import Count, Exists, F, OuterRef, Q, Subquery, Window
from django.db.models.functions import RowNumber

from tests.testapp.models import Address, AddressNote, Person, PersonProfile
from tests.testapp_shared.models import PersonSource


pytestmark = pytest.mark.django_db


@pytest.fixture
def source_row():
    """A row that exists only in the source table, exposed through the view."""
    source = PersonSource.objects.create(first_name="Source", age=40)
    return Person.objects.get(pk=-source.id)


# --------------------------------------------------------------- writes


def test_bulk_update_materialises_a_source_only_row(db_cursor):
    """The copy-on-write path through the update trigger's CASE WHEN merge."""
    source = PersonSource.objects.create(first_name="Src", age=1)
    person = Person.objects.get(pk=-source.id)
    person.age = 77

    Person.objects.bulk_update([person], ["age"])

    assert Person.objects.get(pk=-source.id).age == 77
    assert Person.objects.get(pk=-source.id).first_name == "Src", "untouched column keeps its source value"
    db_cursor.execute("SELECT age FROM person WHERE id = %s", [-source.id])
    assert db_cursor.fetchone() == (77,), "the row is now materialised in the base table"


def test_bulk_update_leaves_the_source_table_alone(db_cursor):
    source = PersonSource.objects.create(first_name="Src", age=1)
    person = Person.objects.get(pk=-source.id)
    person.age = 5

    Person.objects.bulk_update([person], ["age"])

    db_cursor.execute("SELECT age FROM testapp_shared_personsource WHERE id = %s", [source.id])
    assert db_cursor.fetchone() == (1,), "the source is read-only and must stay pristine"


def test_bulk_update_across_a_mix_of_organic_and_source_rows():
    source = PersonSource.objects.create(first_name="Src", age=1)
    organic = Person.objects.create(first_name="Org", age=1)
    rows = [Person.objects.get(pk=-source.id), organic]
    for row in rows:
        row.age = 9

    Person.objects.bulk_update(rows, ["age"])

    assert sorted(Person.objects.values_list("age", flat=True)) == [9, 9]


def test_save_with_update_fields_materialises_only_the_named_column(source_row):
    source_row.age = 42

    source_row.save(update_fields=["age"])

    fresh = Person.objects.get(pk=source_row.pk)
    assert (fresh.age, fresh.first_name) == (42, "Source")


def test_update_reports_the_row_count_for_source_only_rows():
    first = PersonSource.objects.create(first_name="a", age=1)
    second = PersonSource.objects.create(first_name="b", age=1)

    updated = Person.objects.filter(pk__in=[-first.id, -second.id]).update(age=3)

    assert updated == 2, "the trigger fires per row, and Postgres counts each one"


def test_update_reports_a_row_count_even_when_nothing_changes():
    person = Person.objects.create(first_name="x", age=3)

    assert Person.objects.filter(pk=person.pk).update(age=3) == 1


def test_a_second_update_does_not_duplicate_the_materialised_row(db_cursor):
    source = PersonSource.objects.create(first_name="Src", age=1)

    Person.objects.filter(pk=-source.id).update(age=2)
    Person.objects.filter(pk=-source.id).update(age=3)

    db_cursor.execute("SELECT count(*) FROM person WHERE id = %s", [-source.id])
    assert db_cursor.fetchone() == (1,)
    assert Person.objects.get(pk=-source.id).age == 3


def test_get_or_create_finds_a_source_only_row_instead_of_duplicating_it():
    source = PersonSource.objects.create(first_name="Unique Name", age=1)

    person, created = Person.objects.get_or_create(first_name="Unique Name", defaults={"age": 99})

    assert not created
    assert person.pk == -source.id


def test_bulk_create_returns_usable_primary_keys():
    people = Person.objects.bulk_create([Person(first_name=f"p{i}", age=i) for i in range(3)])

    assert all(person.pk is not None for person in people)
    assert set(Person.objects.values_list("pk", flat=True)) == {person.pk for person in people}


def test_refresh_from_db_sees_a_concurrent_update(source_row):
    Person.objects.filter(pk=source_row.pk).update(age=9)

    source_row.refresh_from_db()

    assert source_row.age == 9


# ---------------------------------------------------------------- reads


def test_aggregate_spans_the_union():
    PersonSource.objects.create(first_name="s", age=10)
    Person.objects.create(first_name="o", age=20)

    assert Person.objects.aggregate(n=Count("id"), total=models.Sum("age")) == {"n": 2, "total": 30}


def test_group_by_spans_the_union():
    PersonSource.objects.create(first_name="same", age=1)
    Person.objects.create(first_name="same", age=2)
    Person.objects.create(first_name="other", age=3)

    counts = dict(Person.objects.values_list("first_name").annotate(n=Count("id")).values_list("first_name", "n"))

    assert counts == {"same": 2, "other": 1}


def test_a_materialised_row_is_counted_once_not_twice():
    """The view's anti-join is what stops the base and source halves both
    contributing the same identity."""
    source = PersonSource.objects.create(first_name="s", age=1)
    Person.objects.filter(pk=-source.id).update(age=2)

    assert Person.objects.count() == 1


def test_distinct_on_a_field_spans_the_union():
    PersonSource.objects.create(first_name="dup", age=1)
    Person.objects.create(first_name="dup", age=2)

    assert Person.objects.order_by("first_name", "id").distinct("first_name").count() == 1


def test_window_functions_span_the_union():
    PersonSource.objects.create(first_name="s", age=1)
    Person.objects.create(first_name="o", age=2)

    numbered = list(Person.objects.annotate(rn=Window(RowNumber(), order_by=F("age").asc())).order_by("age"))

    assert [row.rn for row in numbered] == [1, 2]


def test_subquery_and_exists_against_a_related_table():
    person = Person.objects.create(first_name="p", age=1)
    PersonProfile.objects.create(person=person, bio="hello")
    PersonSource.objects.create(first_name="no-profile", age=1)

    annotated = Person.objects.annotate(
        has_profile=Exists(PersonProfile.objects.filter(person=OuterRef("pk"))),
        bio=Subquery(PersonProfile.objects.filter(person=OuterRef("pk")).values("bio")[:1]),
    )

    assert annotated.get(pk=person.pk).has_profile is True
    assert annotated.get(pk=person.pk).bio == "hello"
    assert annotated.exclude(pk=person.pk).get().has_profile is False


def test_filtering_across_a_reverse_relation_reaches_source_rows():
    source = PersonSource.objects.create(first_name="s", age=1)
    PersonProfile.objects.create(person_id=-source.id, bio="from a source row")

    assert Person.objects.filter(profile__bio="from a source row").get().pk == -source.id


def test_select_related_resolves_a_source_backed_target():
    source = PersonSource.objects.create(first_name="Src", age=1)
    profile = PersonProfile.objects.create(person_id=-source.id, bio="b")

    fetched = PersonProfile.objects.select_related("person").get(pk=profile.pk)

    assert fetched.person.first_name == "Src"


def test_prefetch_related_spans_the_union():
    source = AddressNote.objects.create(address=Address.objects.create(street="s", city="c"), text="t")

    addresses = list(Address.objects.filter(pk=source.address_id).prefetch_related("notes"))

    assert [note.text for note in addresses[0].notes.all()] == ["t"]


def test_in_bulk_spans_the_union():
    source = PersonSource.objects.create(first_name="s", age=1)
    organic = Person.objects.create(first_name="o", age=2)

    fetched = Person.objects.in_bulk([-source.id, organic.pk])

    assert {pk: person.first_name for pk, person in fetched.items()} == {-source.id: "s", organic.pk: "o"}


def test_iterator_streams_both_halves():
    PersonSource.objects.create(first_name="s", age=1)
    Person.objects.create(first_name="o", age=2)

    assert sorted(person.first_name for person in Person.objects.iterator(chunk_size=1)) == ["o", "s"]


def test_only_and_defer_load_the_remaining_fields_lazily(source_row):
    assert Person.objects.only("first_name").get(pk=source_row.pk).age == 40
    assert Person.objects.defer("age").get(pk=source_row.pk).first_name == "Source"


def test_union_of_two_overlay_querysets():
    PersonSource.objects.create(first_name="s", age=1)
    Person.objects.create(first_name="o", age=2)

    combined = Person.objects.filter(first_name="s").union(Person.objects.filter(first_name="o"))

    assert sorted(person.first_name for person in combined) == ["o", "s"]


def test_raw_queries_go_through_the_view():
    PersonSource.objects.create(first_name="s", age=1)

    assert [person.first_name for person in Person.objects.raw("SELECT * FROM person_view")] == ["s"]


def test_values_and_values_list_span_the_union():
    PersonSource.objects.create(first_name="s", age=1)
    Person.objects.create(first_name="o", age=2)

    assert sorted(Person.objects.values_list("first_name", flat=True)) == ["o", "s"]
    assert sorted(row["age"] for row in Person.objects.values("age")) == [1, 2]


def test_complex_q_objects_span_the_union():
    PersonSource.objects.create(first_name="s", age=10)
    Person.objects.create(first_name="o", age=99)

    assert Person.objects.filter(Q(age__lt=50) | Q(first_name="o")).count() == 2
    assert Person.objects.filter(Q(age__lt=50) & ~Q(first_name="o")).count() == 1


def test_ordering_and_slicing_span_the_union():
    PersonSource.objects.create(first_name="s", age=10)
    Person.objects.create(first_name="o", age=20)

    assert [person.age for person in Person.objects.order_by("-age")[:2]] == [20, 10]


# ----------------------------------------------------------- round trips


def test_serialization_round_trips_a_source_backed_row(source_row):
    payload = serializers.serialize("json", [source_row])
    Person.objects.filter(pk=source_row.pk).update(age=1)

    for wrapper in serializers.deserialize("json", payload):
        wrapper.save()

    assert Person.objects.get(pk=source_row.pk).age == 40


def test_the_view_is_invisible_to_flush_and_truncate():
    """Django's flush TRUNCATEs everything introspection lists; a view there
    would blow up, and dropping the view's rows would mean the base table."""
    assert "person_view" not in connection.introspection.table_names()
    assert "person" in connection.introspection.table_names()


def test_update_or_create_accepts_defaults_positionally():
    """Django's signature is `update_or_create(defaults=None, **kwargs)` and
    `defaults` may be passed positionally. The overlay wrapper forwards *args,
    so this would break if it only forwarded keywords."""
    person = Person.objects.create(first_name="positional", age=1)

    obj, created = Person.objects.update_or_create({"age": 5}, pk=person.pk)

    assert not created
    assert Person.objects.get(pk=person.pk).age == 5


def test_get_or_create_accepts_defaults_positionally():
    obj, created = Person.objects.get_or_create({"age": 8}, first_name="positional-new")

    assert created
    assert obj.age == 8
