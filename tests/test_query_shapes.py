"""Every ORM shape, against every kind of row the view can hold.

The masked-source FK hole was one uncovered cell of a two-by-two, hiding behind a test
name that claimed the general case. Neither 100% coverage nor 2,200 mutants
could see it: one measures Python lines, the other proves existing code is
load-bearing, and the hole was a *combination of database states* plus a
predicate that was never written.

What finds that class of bug is enumerating the states and crossing them with
the query shapes, rather than picking whichever case was convenient to write.

So: one fixture builds every population a view row can come from, and each test
asserts a shape against `world.visible` — the answer computed from the base and
source tables directly, not from the view. A shape that quietly loses a row, or
returns one twice, has to disagree with that.

Every test runs under both id strategies. NEGATIVE_ID rewrites the primary key
on the way through the view and a uuid strategy does not, which is the single
biggest difference in how a row is addressed — so it is an axis, not a footnote.
"""

from dataclasses import dataclass

import pytest
from django.db import IntegrityError, connection, models, transaction

from django_overlay.strategies import negates_source_ids
from tests.testapp.models import (
    Address,
    AddressUuid4,
    Person,
    PersonNote,
    PersonUuid4,
)
from tests.testapp_shared.models import (
    AddressSource,
    AddressSourceUuid4,
    PersonSource,
    PersonSourceUuid4,
)


pytestmark = pytest.mark.django_db


@dataclass(frozen=True)
class Flavour:
    """One id strategy's worth of models, and how a source row is addressed."""

    label: str
    model: type
    source: type
    address: type
    address_source: type

    def view_id(self, source_row):
        """The pk this source row answers to through the view.

        Derived from the model's own strategy rather than restated, so a test
        cannot disagree with the library about what it is testing.
        """
        if negates_source_ids(self.model._overlay_meta.strategy):
            return -source_row.id
        return source_row.id

    @property
    def base_table(self):
        return self.model.base_table()._meta.db_table

    @property
    def through_table(self):
        return self.model._meta.get_field("addresses").remote_field.through._meta.db_table


FLAVOURS = [
    Flavour("negative_id", Person, PersonSource, Address, AddressSource),
    Flavour("uuid4", PersonUuid4, PersonSourceUuid4, AddressUuid4, AddressSourceUuid4),
]


class World:
    """The five populations, and the truth about what the view should show.

    `visible` is built from what was *put* where, not from querying the view —
    otherwise the assertions would be checking the view against itself.
    """

    def __init__(self, flavour):
        self.flavour = flavour
        model, source = flavour.model, flavour.source

        # 1. organic: only ever existed in the base table
        self.organic = model.objects.create(first_name="organic", age=30)

        # 2. overridden: a vendor row edited here, so it lives in both tables
        shadowed = source.objects.create(first_name="vendor original", age=41)
        self.overridden = model.objects.get(pk=flavour.view_id(shadowed))
        self.overridden.first_name = "overridden"
        self.overridden.save()

        # 3. untouched: a vendor row nobody has touched
        self.untouched_src = source.objects.create(first_name="untouched", age=50)
        self.untouched_pk = flavour.view_id(self.untouched_src)

        # 4. masked: a vendor row deleted here, so a tombstone hides it
        masked_src = source.objects.create(first_name="masked", age=60)
        self.masked_pk = flavour.view_id(masked_src)
        model.objects.get(pk=self.masked_pk).delete()

        # 5. withdrawn: masked here, and then the vendor dropped their row too.
        #    The mask outlives what it was masking -- the state that makes the
        #    FK trigger's tombstone exclusion decide anything.
        withdrawn_src = source.objects.create(first_name="withdrawn", age=70)
        self.withdrawn_pk = flavour.view_id(withdrawn_src)
        model.objects.get(pk=self.withdrawn_pk).delete()
        withdrawn_src.delete()

        self.visible = {
            self.organic.pk: ("organic", 30),
            self.overridden.pk: ("overridden", 41),
            self.untouched_pk: ("untouched", 50),
        }
        self.hidden = {self.masked_pk, self.withdrawn_pk}

    @property
    def names(self):
        return sorted(name for name, _ in self.visible.values())

    @property
    def pks(self):
        return sorted(self.visible)


@pytest.fixture(params=FLAVOURS, ids=lambda f: f.label)
def world(request):
    return World(request.param)


@pytest.fixture
def negative_world():
    """PersonNote is declared against Person, so the two view-to-view FK tests
    below cannot be parametrised -- there is no uuid counterpart of it."""
    return World(FLAVOURS[0])


@pytest.fixture
def model(world):
    """The view model under test, for the shape assertions below."""
    return world.flavour.model


def test_the_fixture_puts_rows_where_it_says_it_does(world, model):
    """The populations are the premise of every test below, so they are
    asserted against the tables directly rather than trusted."""
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id, _overlay_deleted FROM {world.flavour.base_table} ORDER BY id")
        base = dict(cursor.fetchall())
        cursor.execute(f"SELECT count(*) FROM {world.flavour.source._meta.db_table}")
        sources = cursor.fetchone()[0]

    assert base[world.organic.pk] is False
    assert base[world.overridden.pk] is False
    assert base[world.masked_pk] is True, "a tombstone, not a deleted row"
    assert base[world.withdrawn_pk] is True
    assert world.untouched_pk not in base, "never materialised"
    assert sources == 3, "untouched, overridden's original, masked's original"


# ------------------------------------------------------------- reading shapes


def test_all(world, model):
    assert sorted(p.first_name for p in model.objects.all()) == world.names


def test_count_matches_the_row_count(world, model):
    assert model.objects.count() == len(world.visible)


def test_exists(world, model):
    assert model.objects.exists()
    for pk in world.hidden:
        assert not model.objects.filter(pk=pk).exists()


def test_get_by_pk_for_each_visible_row(world, model):
    for pk, (name, _) in world.visible.items():
        assert model.objects.get(pk=pk).first_name == name


def test_get_raises_for_every_hidden_row(world, model):
    for pk in world.hidden:
        with pytest.raises(model.DoesNotExist):
            model.objects.get(pk=pk)


def test_values_list(world, model):
    rows = set(model.objects.values_list("pk", "first_name", "age"))
    assert rows == {(pk, name, age) for pk, (name, age) in world.visible.items()}


def test_values(world, model):
    assert sorted(r["first_name"] for r in model.objects.values("first_name")) == world.names


def test_in_bulk(world, model):
    assert sorted(model.objects.in_bulk()) == world.pks


def test_iterator(world, model):
    assert sorted(p.first_name for p in model.objects.iterator()) == world.names


def test_first_and_last_under_an_order(world, model):
    ordered = model.objects.order_by("first_name")
    assert ordered.first().first_name == world.names[0]
    assert ordered.last().first_name == world.names[-1]


def test_ordered_slicing_pages_the_whole_view(world, model):
    ordered = list(model.objects.order_by("first_name").values_list("first_name", flat=True))
    assert ordered == world.names
    assert ordered[:2] == world.names[:2]
    assert list(model.objects.order_by("first_name")[1:3].values_list("first_name", flat=True)) == world.names[1:3]


def test_distinct_is_not_hiding_a_duplicate(world, model):
    """The anti-join's whole job. If a row ever appeared in both branches,
    distinct() would mask it and every count above would still look right."""
    assert model.objects.distinct().count() == model.objects.count()
    pks = list(model.objects.values_list("pk", flat=True))
    assert len(pks) == len(set(pks))


def test_aggregate_and_annotate(world, model):
    ages = [age for _, age in world.visible.values()]
    assert model.objects.aggregate(n=models.Count("pk"))["n"] == len(ages)
    assert model.objects.aggregate(total=models.Sum("age"))["total"] == sum(ages)
    assert model.objects.aggregate(oldest=models.Max("age"))["oldest"] == max(ages)


def test_filter_and_exclude_partition_the_view(world, model):
    inside = set(model.objects.filter(age__gte=41).values_list("pk", flat=True))
    outside = set(model.objects.exclude(age__gte=41).values_list("pk", flat=True))
    assert inside | outside == set(world.pks)
    assert not (inside & outside)


def test_pk_in_a_list(world, model):
    every = world.pks + sorted(world.hidden)
    assert sorted(model.objects.filter(pk__in=every).values_list("pk", flat=True)) == world.pks


def test_pk_in_a_subquery(world, model):
    inner = model.objects.filter(age__gte=0).values("pk")
    assert sorted(model.objects.filter(pk__in=inner).values_list("pk", flat=True)) == world.pks


# --------------------------------------------------- the origin filters cross


def test_the_origin_filters_partition_the_view(world, model):
    base = set(model.objects.base_only().values_list("pk", flat=True))
    source = set(model.objects.source_only().values_list("pk", flat=True))

    assert base | source == set(world.pks)
    assert not (base & source), "a row cannot come from both branches"


def test_overridden_and_organic_partition_base_only(world, model):
    overridden = set(model.objects.overridden().values_list("pk", flat=True))
    organic = set(model.objects.organic().values_list("pk", flat=True))
    base = set(model.objects.base_only().values_list("pk", flat=True))

    assert overridden | organic == base
    assert not (overridden & organic)
    assert overridden == {world.overridden.pk}
    assert organic == {world.organic.pk}


def test_a_tombstone_is_in_no_origin_slice(world, model):
    """Hidden is hidden, whichever way you ask. A tombstone is a base row, so
    base_only() is the slice most likely to leak one."""
    for slice_ in (
        model.objects.all(),
        model.objects.base_only(),
        model.objects.source_only(),
        model.objects.overridden(),
        model.objects.organic(),
    ):
        leaked = set(slice_.values_list("pk", flat=True)) & world.hidden
        assert not leaked, f"{slice_.query.model.__name__} leaked {leaked}"


def test_with_origin_labels_every_visible_row(world, model):
    labels = {p.pk: p.overlay_origin for p in model.objects.with_origin()}
    assert set(labels) == set(world.pks)
    assert labels[world.organic.pk] == "base"
    assert labels[world.overridden.pk] == "base"
    assert labels[world.untouched_pk] == "source"


# ------------------------------------------------------------- writing shapes


def test_update_touches_every_visible_row_and_no_hidden_one(world, model):
    assert model.objects.update(age=99) == len(world.visible)

    assert set(model.objects.values_list("age", flat=True)) == {99}
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT age FROM {world.flavour.base_table} WHERE _overlay_deleted")
        assert [row[0] for row in cursor.fetchall()] == [60, 70], "tombstones must not be updated"


def test_delete_removes_every_visible_row(world, model):
    model.objects.all().delete()
    assert model.objects.count() == 0


def test_bulk_create_lands_in_the_base_table(world, model):
    model.objects.bulk_create([model(first_name="bulk-1", age=1), model(first_name="bulk-2", age=2)])

    assert model.objects.count() == len(world.visible) + 2
    assert set(model.objects.organic().values_list("first_name", flat=True)) == {"organic", "bulk-1", "bulk-2"}


# ----------------------------------------------- relations, both directions
#
# The interesting discovery here is what is *not* reachable. With the tombstone
# exclusion in place the overlay refuses to create a
# reference to a hidden row, and it already refused to hide a referenced one.
# So "a traversal returns a row the ORM says does not exist" cannot be set up
# through the overlay at all -- which is the guarantee, and is asserted as one.
#
# It is still reachable one way, and only one: the vendor deleting a source row
# out from under a reference. Their table, their DDL, no trigger of ours. That
# case gets its own test rather than being assumed away.

def link(world, person_pk, address_pk):
    """Written straight to the through table rather than through add(), so a
    link to a row the ORM cannot see is at least *attempted* -- which is what
    the refusal test needs."""
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {world.flavour.through_table} (person_id, address_id, label) VALUES (%s, %s, %s)",
            [person_pk, address_pk, "home"],
        )


def test_a_traversal_finds_every_visible_population(world, model):
    """The positive control. Organic, overridden and untouched rows all have to
    survive a join, or the tests below would pass for the wrong reason."""
    address = world.flavour.address.objects.create(street="1 Main", city="Springfield")
    for pk in world.pks:
        link(world, pk, address.pk)

    assert sorted(p.first_name for p in address.people.all()) == world.names
    reachable = model.objects.filter(addresses__city="Springfield")
    assert sorted(reachable.values_list("first_name", flat=True)) == world.names


def test_a_hidden_row_cannot_be_referenced_from_either_direction(world, model, db_cursor):
    """Both halves of the invariant in one place.

    Forward: a link to a masked person is refused when written. Backward:
    masking a person that is already linked is refused. Between them there is
    no way to reach the state the traversal tests would otherwise need.

    Both triggers are deferred to COMMIT, as Django's own foreign keys are on
    PostgreSQL, and a test transaction never commits -- so each check is forced
    with SET CONSTRAINTS rather than waiting for a COMMIT that will not come.
    """
    address = world.flavour.address.objects.create(street="2 Main", city="Shelbyville")
    db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    db_cursor.execute("SET CONSTRAINTS ALL DEFERRED")

    with pytest.raises(IntegrityError, match="not found in any target table"):
        with transaction.atomic():
            link(world, world.masked_pk, address.pk)
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    # Flush the link's own insert-side check while the person is still visible,
    # so the assertion below can only be catching the delete-side guard. Left
    # pending, SET CONSTRAINTS ALL IMMEDIATE fires both and the insert-side one
    # reports first -- the write is refused either way, but by the other guard
    # and with a message about the wrong thing.
    db_cursor.execute("SET CONSTRAINTS ALL DEFERRED")
    link(world, world.organic.pk, address.pk)
    db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    db_cursor.execute("SET CONSTRAINTS ALL DEFERRED")

    with pytest.raises(IntegrityError, match="still references"):
        with transaction.atomic():
            db_cursor.execute(
                f"UPDATE {world.flavour.base_table} SET _overlay_deleted = TRUE WHERE id = %s",
                [world.organic.pk],
            )
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.django_db(transaction=True)
def test_a_vendor_withdrawal_leaves_no_phantom_in_a_traversal(world, model):
    """The one dangling reference the library cannot prevent, because it is not
    ours to prevent: the vendor drops a row we point at.

    Nothing in the overlay can refuse that -- the source table has no trigger of
    ours on it. What the overlay must still do is not hand back a row the view
    does not contain.

    transaction=True, and that detail is the finding. Inside one transaction
    this state cannot even be built: the link's own FK check is deferred to
    COMMIT, so it runs *after* the vendor's delete and rejects it. Production
    does not work that way -- the link was committed days earlier and its check
    passed at the time -- so reproducing the real thing needs a real commit
    between the two.
    """
    src = world.flavour.address_source.objects.create(street="3 Main", city="Ogdenville")
    withdrawn_pk = world.flavour.view_id(src)
    link(world, world.organic.pk, withdrawn_pk)
    assert [a.city for a in world.organic.addresses.all()] == ["Ogdenville"]

    src.delete()  # the vendor's refresh, straight into their own table

    assert list(world.organic.addresses.all()) == []
    assert not model.objects.filter(addresses__city="Ogdenville").exists()
    assert not world.flavour.address.objects.filter(pk=withdrawn_pk).exists()


def test_a_reverse_fk_crosses_two_views_intact(negative_world):
    """PersonNote is itself an overlay model, so this is view-to-view."""
    for person in (negative_world.organic, negative_world.overridden):
        PersonNote.objects.create(person=person, text=f"note for {person.first_name}")

    assert [n.text for n in negative_world.organic.overlay_notes.all()] == ["note for organic"]
    assert sorted(PersonNote.objects.values_list("text", flat=True)) == [
        "note for organic",
        "note for overridden",
    ]
    assert [n.text for n in PersonNote.objects.filter(person__first_name="organic")] == ["note for organic"]
    assert not PersonNote.objects.filter(person__first_name="masked").exists()


def test_select_related_and_prefetch_agree_across_the_populations(negative_world):
    """select_related() is routed to prefetch_related() for overlay paths, so
    the two spellings have to return the same rows."""
    for person in (negative_world.organic, negative_world.overridden):
        PersonNote.objects.create(person=person, text=f"note for {person.first_name}")

    selected = {n.pk: n.person.first_name for n in PersonNote.objects.select_related("person")}
    prefetched = {n.pk: n.person.first_name for n in PersonNote.objects.prefetch_related("person")}
    plain = {n.pk: n.person.first_name for n in PersonNote.objects.all()}

    assert selected == prefetched == plain
    assert sorted(selected.values()) == ["organic", "overridden"]


def test_prefetch_related_across_the_m2m_for_every_population(world, model):
    address = world.flavour.address.objects.create(street="4 Main", city="North Haverbrook")
    for pk in world.pks:
        link(world, pk, address.pk)

    prefetched = {p.pk: [a.city for a in p.addresses.all()] for p in model.objects.prefetch_related("addresses")}

    assert set(prefetched) == set(world.pks)
    assert all(cities == ["North Haverbrook"] for cities in prefetched.values())
