"""Relation traversal in filters, across every join shape the library allows.

A join through an overlay model is a join against a view, and the planner has
to reach source-backed rows through the anti-join to find them. The failure
mode this file guards against is silent: a join that quietly returns only the
materialised half still returns rows, so it looks like it works.

Every test therefore puts the row it is looking for **in the source table
only**, and asserts the join finds it. Where both ends of a join can be source
backed, both are.

The join shapes, and where each is exercised:

    plain    -> overlay   forward FK      AddressNote.address
    overlay  -> plain     reverse FK      Address.notes
    overlay  -> overlay   forward FK      PersonNote.person
    overlay  -> overlay   reverse FK      Person.overlay_notes
    overlay <-> overlay   M2M both ways   Person.addresses / Address.people
    overlay  -> plain     reverse O2O     Person.profile
    three deep                            Person -> Address -> AddressNote
"""

import pytest
from django.db.models import Count, Q

from tests.testapp.models import (
    Address,
    AddressNote,
    Person,
    PersonAddressThrough,
    PersonNote,
    PersonProfile,
)
from tests.testapp_shared.models import AddressSource, PersonSource


pytestmark = pytest.mark.django_db


@pytest.fixture
def source_person():
    """A person that exists only in the source table."""
    source = PersonSource.objects.create(first_name="SourcePerson", age=40)
    return Person.objects.get(pk=-source.id)


@pytest.fixture
def source_address():
    """An address that exists only in the source table."""
    source = AddressSource.objects.create(street="Source Street", city="Sourceville")
    return Address.objects.get(pk=-source.id)


# ------------------------------------------------- forward FK, plain -> overlay


def test_forward_fk_filter_reaches_a_source_backed_target(source_address):
    """AddressNote is a plain table; the join target is a view row that exists
    only in the source."""
    note = AddressNote.objects.create(address=source_address, text="note text")

    found = AddressNote.objects.filter(address__city="Sourceville")

    assert [n.pk for n in found] == [note.pk]


def test_forward_fk_filter_spans_both_halves(source_address):
    organic = Address.objects.create(street="Organic Street", city="Sourceville")
    from_source = AddressNote.objects.create(address=source_address, text="a")
    from_base = AddressNote.objects.create(address=organic, text="b")

    found = AddressNote.objects.filter(address__city="Sourceville")

    assert {n.pk for n in found} == {from_source.pk, from_base.pk}


# ------------------------------------------------ reverse FK, overlay -> plain


def test_reverse_fk_filter_finds_a_source_backed_owner(source_address):
    AddressNote.objects.create(address=source_address, text="findme")

    assert Address.objects.filter(notes__text="findme").get().pk == source_address.pk


def test_reverse_fk_exclude_takes_the_subquery_path(source_address):
    """exclude() across a multi-valued relation compiles to NOT (subquery),
    which is a different code path from filter()."""
    other = Address.objects.create(street="s", city="c")
    AddressNote.objects.create(address=source_address, text="findme")

    remaining = Address.objects.exclude(notes__text="findme")

    assert source_address.pk not in {a.pk for a in remaining}
    assert other.pk in {a.pk for a in remaining}


# ---------------------------------------------- forward FK, overlay -> overlay


def test_overlay_to_overlay_forward_fk_filter(source_person):
    """Both ends are views: PersonNote's base table joined to person_view."""
    note = PersonNote.objects.create(person=source_person, text="t")

    assert PersonNote.objects.filter(person__first_name="SourcePerson").get().pk == note.pk


def test_overlay_to_overlay_reverse_fk_filter(source_person):
    PersonNote.objects.create(person=source_person, text="reverse")

    assert Person.objects.filter(overlay_notes__text="reverse").get().pk == source_person.pk


# ------------------------------------------------------- many to many, view/view


def test_m2m_forward_filter_with_both_ends_source_backed(source_person, source_address):
    """The hardest shape: two views joined through a plain through table, with
    the matching row on each side living only in its source."""
    PersonAddressThrough.objects.create(person=source_person, address=source_address)

    assert Person.objects.filter(addresses__city="Sourceville").get().pk == source_person.pk


def test_m2m_reverse_filter_with_both_ends_source_backed(source_person, source_address):
    PersonAddressThrough.objects.create(person=source_person, address=source_address)

    assert Address.objects.filter(people__first_name="SourcePerson").get().pk == source_address.pk


def test_m2m_filter_does_not_drop_the_materialised_half(source_address):
    organic = Person.objects.create(first_name="Organic", age=1)
    source = Person.objects.get(pk=-PersonSource.objects.create(first_name="Sourced", age=2).pk)
    PersonAddressThrough.objects.create(person=organic, address=source_address)
    PersonAddressThrough.objects.create(person=source, address=source_address)

    found = Person.objects.filter(addresses__city="Sourceville")

    assert {p.first_name for p in found} == {"Organic", "Sourced"}


# ----------------------------------------------------------------- three deep


def test_three_level_join_crosses_two_views_and_a_plain_table(source_person, source_address):
    PersonAddressThrough.objects.create(person=source_person, address=source_address)
    AddressNote.objects.create(address=source_address, text="deep")

    assert Person.objects.filter(addresses__notes__text="deep").get().pk == source_person.pk


# ---------------------------------------------------------- combining lookups


def test_q_or_across_two_different_joins(source_person, source_address):
    PersonAddressThrough.objects.create(person=source_person, address=source_address)
    other = Person.objects.create(first_name="Other", age=9)
    PersonProfile.objects.create(person=other, bio="via profile")

    found = Person.objects.filter(Q(addresses__city="Sourceville") | Q(profile__bio="via profile")).distinct()

    assert {p.pk for p in found} == {source_person.pk, other.pk}


def test_join_filter_combined_with_a_local_filter(source_person, source_address):
    PersonAddressThrough.objects.create(person=source_person, address=source_address)

    assert Person.objects.filter(addresses__city="Sourceville", age=40).get().pk == source_person.pk
    assert not Person.objects.filter(addresses__city="Sourceville", age=41).exists()


def test_annotate_count_over_a_join_counts_source_rows(source_person, source_address):
    PersonAddressThrough.objects.create(person=source_person, address=source_address)
    PersonAddressThrough.objects.create(person=source_person, address=Address.objects.create(street="s", city="c"))

    annotated = Person.objects.annotate(n=Count("addresses")).get(pk=source_person.pk)

    assert annotated.n == 2


def test_order_by_across_a_join(source_person):
    later = Address.objects.create(street="s", city="Zed")
    PersonAddressThrough.objects.create(person=source_person, address=later)
    earlier = Address.objects.create(street="s", city="Alpha")
    PersonAddressThrough.objects.create(person=source_person, address=earlier)

    cities = list(
        Person.objects.filter(pk=source_person.pk).order_by("addresses__city").values_list("addresses__city", flat=True)
    )

    assert cities == ["Alpha", "Zed"]


def test_values_list_across_a_join_reads_source_columns(source_person, source_address):
    PersonAddressThrough.objects.create(person=source_person, address=source_address)

    assert list(Person.objects.filter(pk=source_person.pk).values_list("addresses__street", flat=True)) == [
        "Source Street"
    ]


def test_join_filter_after_the_target_is_materialised(source_person, source_address):
    """Copy-on-write moves the target row from the source into the base table.
    The join has to keep finding it, and must not find it twice."""
    PersonAddressThrough.objects.create(person=source_person, address=source_address)
    source_address.city = "Renamed"
    source_address.save()

    assert not Person.objects.filter(addresses__city="Sourceville").exists()
    assert [p.pk for p in Person.objects.filter(addresses__city="Renamed")] == [source_person.pk]
