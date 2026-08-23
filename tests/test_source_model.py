"""Reading the vendor's own row.

The view is the only ORM entry point an overlay model has, and it cannot show
you everything: its anti-join drops any source row a base row shadows, so the
vendor's original values for a row you have overridden are not reachable
through it. `reset_to_source()` could get them, by destroying the override.

`source_table()` and `source_row()` make that a read.
"""

import pytest
from django.db import connection

from tests.testapp.models import FilteredSourceTest, MetaTest, Person, PersonUuid4, RosterMembership
from tests.testapp_shared.models import FilteredSourceTestSource, PersonSource, PersonSourceUuid4


pytestmark = pytest.mark.django_db


# ------------------------------------------------------- the thing it is for


def test_it_reads_the_vendors_original_behind_an_override():
    """The gap this closes. Once a row is overridden the view shows only the
    tenant's version, and the anti-join means source_only() cannot reach the
    original either."""
    vendor = PersonSource.objects.create(first_name="vendor original", age=41)
    person = Person.objects.get(pk=-vendor.id)
    person.first_name = "edited here"
    person.save()

    assert person.source_row().first_name == "vendor original"
    assert Person.objects.get(pk=-vendor.id).first_name == "edited here"
    assert not Person.objects.source_only().filter(pk=-vendor.id).exists(), (
        "the premise: the view cannot show the original"
    )


def test_source_row_for_an_untouched_row_is_the_same_row():
    vendor = PersonSource.objects.create(first_name="untouched", age=50)

    assert Person.objects.get(pk=-vendor.id).source_row().first_name == "untouched"


def test_source_row_is_none_for_a_row_the_vendor_never_had():
    assert Person.objects.create(first_name="ours", age=1).source_row() is None


def test_source_row_survives_the_row_being_masked():
    """A tombstone hides the source row from the view. It does not remove it,
    and this is how you can still tell what is being hidden."""
    vendor = PersonSource.objects.create(first_name="masked", age=60)
    person = Person.objects.get(pk=-vendor.id)
    person.delete()

    assert not Person.objects.filter(pk=-vendor.id).exists()
    assert Person(pk=-vendor.id).source_row().first_name == "masked"


def test_a_uuid_strategy_needs_no_negation():
    vendor = PersonSourceUuid4.objects.create(first_name="vendor", age=30)

    assert PersonUuid4.objects.get(pk=vendor.id).source_row().first_name == "vendor"


# --------------------------------------------------------------- the model


def test_the_model_is_cached_so_rows_compare_equal():
    """Rebuilding the type per call would make two reads of one row unequal."""
    assert Person.source_table() is Person.source_table()

    vendor = PersonSource.objects.create(first_name="v", age=1)
    first = Person.source_table().objects.get(pk=vendor.id)
    second = Person.source_table().objects.get(pk=vendor.id)
    assert first == second


def test_it_maps_the_vendors_columns_and_drops_the_shadow_flag():
    fields = [f.name for f in Person.source_table()._meta.concrete_fields]

    assert fields == ["id", "first_name", "age"]
    assert "_overlay_deleted" not in fields, "the flag is the base table's, not the vendor's"
    assert Person.source_table()._meta.db_table == '"public"."testapp_shared_personsource"'
    assert Person.source_table()._meta.managed is False


def test_a_relation_column_becomes_a_plain_column():
    """A vendor table holds an id, not a Django relation. Pointing a real FK at
    an overlay model would drag the view machinery in behind it.

    And it must not stay an auto field: a foreign key column holds whatever the
    referenced row's pk happens to be, and a second auto-generated field makes
    Django refuse the model outright."""
    fields = {f.name: f for f in RosterMembership.source_table()._meta.concrete_fields}

    assert not fields["roster_id"].is_relation
    assert not fields["member_id"].is_relation
    assert fields["id"].primary_key
    assert sum(1 for f in fields.values() if getattr(f, "auto_created", False) and f.primary_key) <= 1


def test_a_warm_base_model_does_not_poison_the_source_model():
    """Regression, and the reason the fields are built with clone().

    Django's Field.__deepcopy__ is a shallow copy, so a copied field shares
    `cached_col` with the original -- and once anything has queried the base
    model that cache holds a column qualified with the *base* table. The source
    model then generated `SELECT "person"."age" FROM
    "public"."testapp_shared_personsource"`, which passes in isolation and
    fails the moment any other test runs first. The rebuild below is what makes
    this test see it at all.
    """
    list(Person.objects.all())  # warms cached_col on the base model's fields
    if "_source_model" in Person.__dict__:
        delattr(Person, "_source_model")

    vendor = PersonSource.objects.create(first_name="warm", age=2)
    sql = str(Person.source_table().objects.all().query)

    assert '"person"."' not in sql, f"a base-table column leaked into the source query: {sql}"
    assert Person.source_table().objects.get(pk=vendor.id).first_name == "warm"


def test_it_is_invisible_to_the_app_registry():
    """The reason for the private Apps(): a project that declares its own
    PersonSource must not end up with two models claiming one db_table, which
    is a models.W035 and a real diagnostic wasted."""
    from django.apps import apps

    generated = Person.source_table()
    assert generated not in apps.get_models()
    assert not any(m.__name__ == generated.__name__ for m in apps.get_models())


# ------------------------------------------------------------- read-only


def test_writing_through_the_source_model_is_refused():
    vendor = PersonSource.objects.create(first_name="vendor", age=1)
    row = Person.source_table().objects.get(pk=vendor.id)

    with pytest.raises(NotImplementedError, match="read-only"):
        row.save()
    with pytest.raises(NotImplementedError, match="read-only"):
        row.delete()


# --------------------------------------------------------- extra_where


def test_extra_where_is_applied_by_default():
    """The view splices `active` into its source branch, so a source model that
    ignored it would report rows the overlay treats as nonexistent."""
    FilteredSourceTestSource.objects.create(first_name="visible", active=True)
    FilteredSourceTestSource.objects.create(first_name="hidden", active=False)

    model = FilteredSourceTest.source_table()

    assert sorted(model.objects.values_list("first_name", flat=True)) == ["visible"]
    assert sorted(model.objects.unfiltered().values_list("first_name", flat=True)) == ["hidden", "visible"]


def test_the_default_agrees_with_the_view():
    FilteredSourceTestSource.objects.create(first_name="visible", active=True)
    FilteredSourceTestSource.objects.create(first_name="hidden", active=False)

    through_the_view = set(FilteredSourceTest.objects.values_list("first_name", flat=True))
    through_the_source = set(FilteredSourceTest.source_table().objects.values_list("first_name", flat=True))

    assert through_the_view == through_the_source == {"visible"}
