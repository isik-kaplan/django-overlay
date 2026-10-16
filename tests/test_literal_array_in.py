"""A literal key list sent as one array parameter: when it fires, and that it never changes rows.

`OverlayLiteralArrayIn` turns `pk__in=[u1, u2, …]` into `pk = ANY (%s::uuid[])`
with the whole list as one parameter. Postgres plans the two identically, so
what has to hold is the rows -- checked here against Django's own `IN`, with
the setting off -- and the guards, each of which is a shape where skipping
Django's per-value preparation would be wrong or where the plan would change.
"""

import uuid
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db.models import F, UUIDField
from django.db.models.functions import Coalesce
from django.test import override_settings

from django_overlay.fields import OverlayLiteralArrayIn
from tests.testapp.models import NullableFkOverlay, Person, PersonUuid7Polyfill, WideCustomer, WideOrder
from tests.testapp_shared.models import PersonSourceUuid7Polyfill


pytestmark = pytest.mark.django_db

OFF = override_settings(DJANGO_OVERLAY_ARRAY_LITERAL_IN=False)


def sql_of(queryset):
    sql, params = queryset.query.sql_with_params()
    return sql, params


def rewritten(queryset):
    return "= ANY (%s::" in sql_of(queryset)[0]


def both_ways(build):
    """(with the rewrite, with Django's IN) -- built inside each setting, so each
    compiles under the configuration it is meant to measure. Sorted with None
    first, because a nullable key comes back as None."""
    order = lambda value: (value is not None, value)  # noqa: E731
    on = sorted(build(), key=order)
    with OFF:
        off = sorted(build(), key=order)
    return on, off


@pytest.fixture
def people():
    """uuid keys from both branches of the view: two vendor rows, two organic."""
    sources = [PersonSourceUuid7Polyfill.objects.create(first_name=f"v{n}", age=n) for n in range(2)]
    vendor = [PersonUuid7Polyfill.objects.get(pk=source.id) for source in sources]
    organic = [PersonUuid7Polyfill.objects.create(first_name=f"o{n}") for n in range(2)]
    return vendor + organic


# ---------------------------------------------------------------- it fires


def test_a_uuid_key_list_is_one_array_parameter(people):
    keys = [person.pk for person in people[:3]]
    queryset = PersonUuid7Polyfill.objects.filter(pk__in=keys)

    sql, params = sql_of(queryset)
    assert "= ANY (%s::uuid[])" in sql
    (literal,) = [p for p in params if isinstance(p, str) and p.startswith("{")]
    assert sorted(literal.strip("{}").split(",")) == sorted(str(k) for k in keys)


def test_it_returns_what_django_does_across_both_branches(people):
    keys = [people[0].pk, people[2].pk, people[2].pk, uuid.uuid4()]  # a duplicate, and a key nobody has

    on, off = both_ways(lambda: PersonUuid7Polyfill.objects.filter(pk__in=keys).values_list("first_name", flat=True))

    assert on == off == ["o0", "v0"]


def test_a_negative_integer_key_survives_the_literal():
    """NEGATIVE_ID: a vendor row's key is negative in the view, and `{-3,7}` has
    to read back as two integers, not a malformed literal."""
    vendor = WideCustomer.objects.create(first_name="a", last_name="positive", email="a@x", age=1)
    keys = [vendor.pk, -1 * vendor.pk - 1000]
    queryset = WideCustomer.objects.filter(pk__in=keys)

    assert "= ANY (%s::integer[])" in sql_of(queryset)[0]
    on, off = both_ways(lambda: WideCustomer.objects.filter(pk__in=keys).values_list("last_name", flat=True))
    assert on == off == ["positive"]


def test_a_foreign_key_column_list_uses_the_target_type():
    customers = [
        WideCustomer.objects.create(first_name=f"c{n}", last_name=f"c{n}", email=f"{n}@x", age=n) for n in range(3)
    ]
    for n, customer in enumerate(customers):
        WideOrder.objects.create(
            reference=f"R{n}", status="new", total_cents=1, channel="web", currency="GBP", customer=customer
        )
    keys = [customers[0].pk, customers[2].pk]

    assert rewritten(WideOrder.objects.filter(customer_id__in=keys))
    on, off = both_ways(lambda: WideOrder.objects.filter(customer_id__in=keys).values_list("reference", flat=True))
    assert on == off == ["R0", "R2"]


@pytest.mark.parametrize("container", [list, tuple, set, frozenset])
def test_any_python_collection_is_accepted(people, container):
    keys = container(person.pk for person in people[:2])
    assert rewritten(PersonUuid7Polyfill.objects.filter(pk__in=keys))


def test_excluding_a_list_agrees_with_django_including_a_null_key():
    """NOT (fk = ANY (...)) and NOT (fk IN (...)) are the same three-valued
    logic, and Django adds its own IS NULL branch around either."""
    one, two = Person.objects.create(first_name="a"), Person.objects.create(first_name="b")
    for person in (one, two, None):
        NullableFkOverlay.objects.create(person=person)

    keys = [one.pk, two.pk + 1000]
    on, off = both_ways(
        lambda: NullableFkOverlay.objects.exclude(person_id__in=keys).values_list("person_id", flat=True)
    )

    assert rewritten(NullableFkOverlay.objects.exclude(person_id__in=keys))
    assert on == off
    assert len(on) == 2, "the other person's row and the null one"


# ---------------------------------------------------------------- when it must not


@pytest.mark.parametrize("keys", [lambda p: [p[0].pk], lambda p: [p[0].pk, p[0].pk]])
def test_one_distinct_value_is_left_to_django(people, keys):
    """`IN (x)` plans as `= x`; `= ANY ('{x}')` stays an array comparison."""
    assert not rewritten(PersonUuid7Polyfill.objects.filter(pk__in=keys(people)))


def test_an_empty_list_is_left_to_django(people):
    assert list(PersonUuid7Polyfill.objects.filter(pk__in=[])) == []


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(lambda p: [str(p[0].pk), str(p[1].pk)], id="strings"),
        pytest.param(lambda p: [p[0].pk, str(p[1].pk)], id="mixed"),
    ],
)
def test_values_that_are_not_the_columns_type_are_left_to_django(people, values):
    """Only a value already of the column's type can skip Django's prep and go
    into the literal as its plain text form."""
    keys = values(people)
    queryset = PersonUuid7Polyfill.objects.filter(pk__in=keys)

    assert not rewritten(queryset)
    assert sorted(p.pk for p in queryset) == sorted([people[0].pk, people[1].pk])


def test_model_instances_on_a_foreign_key_are_left_to_django():
    """A foreign key's `__in` converts instances to keys in RelatedIn; that is
    Django's job, and the values are not the column's type."""
    customers = [
        WideCustomer.objects.create(first_name=f"c{n}", last_name=f"c{n}", email=f"{n}@x", age=n) for n in range(2)
    ]
    for n, customer in enumerate(customers):
        WideOrder.objects.create(
            reference=f"R{n}", status="new", total_cents=1, channel="web", currency="GBP", customer=customer
        )
    queryset = WideOrder.objects.filter(customer__in=customers)

    assert not rewritten(queryset)
    assert sorted(queryset.values_list("reference", flat=True)) == ["R0", "R1"]


def test_a_bool_is_not_an_integer_key():
    """`type(v) is int`, not isinstance: True must not become key 1."""
    assert not rewritten(WideCustomer.objects.filter(pk__in=[True, 2]))


def test_a_column_that_is_not_a_key_type_is_left_to_django(people):
    assert not rewritten(PersonUuid7Polyfill.objects.filter(first_name__in=["v0", "o1"]))


def test_an_annotation_that_is_just_the_column_is_still_the_column(people):
    keys = [people[0].pk, people[1].pk]
    assert rewritten(PersonUuid7Polyfill.objects.annotate(key=F("pk")).filter(key__in=keys))


def test_an_expression_on_the_left_is_left_to_django(people):
    keys = [people[0].pk, people[1].pk]
    queryset = PersonUuid7Polyfill.objects.annotate(key=Coalesce("pk", "pk")).filter(key__in=keys)
    assert not rewritten(queryset)
    assert sorted(p.pk for p in queryset) == sorted(keys)


def test_a_subquery_is_not_a_literal_list(people):
    queryset = PersonUuid7Polyfill.objects.filter(pk__in=PersonUuid7Polyfill.objects.values("pk"))
    assert not rewritten(queryset)


def test_the_setting_turns_it_off(people):
    with OFF:
        assert not rewritten(PersonUuid7Polyfill.objects.filter(pk__in=[people[0].pk, people[1].pk]))


def test_a_non_bool_setting_is_refused(people):
    with override_settings(DJANGO_OVERLAY_ARRAY_LITERAL_IN="yes"):
        with pytest.raises(ImproperlyConfigured, match="DJANGO_OVERLAY_ARRAY_LITERAL_IN must be a bool"):
            PersonUuid7Polyfill.objects.filter(pk__in=[people[0].pk, people[1].pk])


def test_the_lookup_skips_per_value_preparation(people):
    """The point of it. Django's In runs each value through the field's prep
    when the lookup is built and again when it compiles; this does neither."""
    keys = [people[0].pk, people[1].pk]
    with mock.patch.object(UUIDField, "get_db_prep_value", wraps=UUIDField().get_db_prep_value) as prep:
        queryset = PersonUuid7Polyfill.objects.filter(pk__in=keys)
        sql_of(queryset)

    assert isinstance(queryset.query.where.children[0], OverlayLiteralArrayIn)
    assert prep.call_count == 0

    with OFF, mock.patch.object(UUIDField, "get_db_prep_value", wraps=UUIDField().get_db_prep_value) as prep:
        sql_of(PersonUuid7Polyfill.objects.filter(pk__in=keys))
    assert prep.call_count == len(keys), "the control: Django's In does call it, once per value"
