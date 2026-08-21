"""`fk__in=<subquery>` compiles to `= ANY (ARRAY(...))` — TODO/06.

A `UNION ALL` view is an appendrel with no parent statistics, so any join
between two of them is estimated blind; `LIMIT` then turns that into a nested
loop that never terminates early. `IN (subquery)` stays a semi-join and is
costed with the broken estimate. `ARRAY(subquery)` becomes an InitPlan,
evaluated before the outer plan is costed.

Measured at 900,000 view rows against a 1.6ms plain-table baseline: the
traversal Django emits today is 6,868.9ms, `IN (subquery)` is 727.4ms, this is
**3.9ms**. See tests/probe_join_fixes.py.

The rewrite has to be invisible, so most of this file is about it *not*
changing behaviour.
"""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import override_settings

from tests.testapp.models import Address, Person, PersonNote, WideCustomerU7, WideOrderU7
from tests.testapp_shared.models import PersonSource


pytestmark = pytest.mark.django_db


def where(queryset) -> str:
    sql = str(queryset.query)
    return sql[sql.index(" WHERE ") :] if " WHERE " in sql else ""


# ------------------------------------------------------------ the rewrite


def test_a_subquery_becomes_an_array():
    inner = WideCustomerU7.objects.filter(city="city42").values("id")
    clause = where(WideOrderU7.objects.filter(customer_id__in=inner))

    assert "= ANY (ARRAY(SELECT" in clause
    assert " IN (SELECT" not in clause


def test_passing_a_queryset_directly_is_rewritten_too():
    inner = WideCustomerU7.objects.filter(city="city42")
    assert "= ANY (ARRAY(SELECT" in where(WideOrderU7.objects.filter(customer__in=inner))


def test_a_literal_list_is_left_alone():
    """There is no subquery to fence, and Django's IN is already right."""
    clause = where(WideOrderU7.objects.filter(customer_id__in=[1, 2, 3]))

    assert "ANY" not in clause
    assert " IN (" in clause


def test_exclude_negates_the_whole_thing():
    inner = WideCustomerU7.objects.filter(city="city42").values("id")
    clause = where(WideOrderU7.objects.exclude(customer_id__in=inner))

    assert "NOT (" in clause
    assert "= ANY (ARRAY(SELECT" in clause


def test_a_plain_foreign_key_is_untouched():
    """Registered on OverlayForeignKey only. `WideCustomer.region` is a plain
    ForeignKey to a plain table, so even though the outer relation is a view,
    the subquery side has real statistics and rescues the estimate — measured
    at 1.2-1.3x for view -> plain. Nothing to fence."""
    from tests.testapp.models import WideRegion

    inner = WideRegion.objects.values("id")
    clause = where(WideCustomerU7.objects.filter(region_id__in=inner))

    assert "ANY" not in clause
    assert " IN (SELECT" in clause


# --------------------------------------------------- it must not change results


@pytest.fixture
def people():
    source = PersonSource.objects.create(first_name="Vendor", age=40)
    organic = Person.objects.create(first_name="Organic", age=41)
    return {"source": Person.objects.get(pk=-source.id), "organic": organic}


def test_the_answers_are_unchanged(people):
    inner = Person.objects.filter(age__gte=40).values("id")

    matched = set(Person.objects.filter(id__in=[p.pk for p in people.values()]).values_list("first_name", flat=True))
    assert matched == {"Vendor", "Organic"}
    assert Person.objects.filter(pk__in=inner).count() == 2


def test_a_foreign_key_subquery_returns_the_right_rows(people):
    """The shape the rewrite exists for, across two overlay models."""
    for person in people.values():
        PersonNote.objects.create(person=person, text=f"note for {person.first_name}")

    inner = Person.objects.filter(first_name="Vendor").values("id")
    notes = PersonNote.objects.filter(person_id__in=inner)

    assert [note.text for note in notes] == ["note for Vendor"]


def test_an_empty_subquery_matches_nothing(people):
    inner = Person.objects.filter(first_name="nobody at all").values("id")

    assert PersonNote.objects.filter(person_id__in=inner).count() == 0


def test_exclude_returns_the_complement(people):
    for person in people.values():
        PersonNote.objects.create(person=person, text=person.first_name)

    inner = Person.objects.filter(first_name="Vendor").values("id")

    assert [n.text for n in PersonNote.objects.exclude(person_id__in=inner)] == ["Organic"]


def test_filtering_by_model_instances_still_works(people):
    """`RelatedIn` is what converts an instance to its pk. Subclassing plain
    `In` instead silently broke this, so it is pinned."""
    for person in people.values():
        PersonNote.objects.create(person=person, text=person.first_name)

    notes = PersonNote.objects.filter(person__in=[people["source"]])

    assert [note.text for note in notes] == ["Vendor"]


def test_it_survives_a_join_and_an_order_by(people):
    address = Address.objects.create(street="s", city="c")
    people["source"].addresses.add(address)

    inner = Address.objects.filter(city="c").values("id")
    matched = Person.objects.filter(addresses__id__in=inner).order_by("id")

    assert [p.first_name for p in matched] == ["Vendor"]


def test_the_generated_sql_actually_executes(people):
    """A rewrite that only looks right in str(query) is worth nothing."""
    inner = Person.objects.filter(age__gte=40).values("id")
    with connection.cursor() as cursor:
        sql, params = PersonNote.objects.filter(person_id__in=inner).query.get_compiler("default").as_sql()
        cursor.execute(sql, params)
        cursor.fetchall()


# ------------------------------------------------------------- the opt-out


@override_settings(DJANGO_OVERLAY_ARRAY_SUBQUERY_IN=False)
def test_it_can_be_turned_off():
    inner = WideCustomerU7.objects.filter(city="city42").values("id")
    clause = where(WideOrderU7.objects.filter(customer_id__in=inner))

    assert "ANY" not in clause
    assert " IN (SELECT" in clause


@override_settings(DJANGO_OVERLAY_ARRAY_SUBQUERY_IN=False)
def test_this_flag_no_longer_reaches_the_m2m_fence():
    """The two used to share one setting, and sharing it was the bug.

    Turning it off to spare one broad `fk__in` also unfenced every m2m
    traversal -- 306.6ms -> 0.4ms selective, 7,896.5ms -> 105.9ms broad, given
    away to fix something unrelated. The fence has DJANGO_OVERLAY_M2M_FENCE of
    its own now, so this flag stops at the foreign-key lookup.
    """
    from tests.testapp.models import Roster

    fenced = Roster.objects.filter(members__name="m")

    assert "ANY (ARRAY" in str(fenced.query), "the fence keeps its array form"
    assert list(fenced) == [], "and it still executes"


@override_settings(DJANGO_OVERLAY_M2M_FENCE=False)
def test_the_fence_has_a_flag_of_its_own_now():
    """And it gates whether the fence is added, not how it compiles.

    A fence compiled as a plain `IN` is the one combination with no argument
    for it: an extra semi-join costed with the same blind appendrel estimate
    the fence exists to route around. So the choice is the fence or nothing.
    """
    from tests.testapp.models import Roster

    fenced = Roster.objects.filter(members__name="m")
    clause = str(fenced.query)

    assert "overlay_fenced_in" not in clause
    assert "ANY (ARRAY" not in clause
    assert list(fenced) == [], "and it still executes"


@override_settings(DJANGO_OVERLAY_M2M_FENCE="please")
def test_the_fence_flag_refuses_a_non_boolean_like_every_other():
    from tests.testapp.models import Roster

    with pytest.raises(ImproperlyConfigured, match="must be a bool"):
        str(Roster.objects.filter(members__name="m").query)


def test_the_fenced_lookup_declines_a_literal_scope():
    """The one branch of OverlayFencedIn.as_sql that reads no setting.

    `_m2m_fence()` always builds a queryset, so this is reachable only by
    naming the lookup by hand -- which is the supported way to fence a scope
    the library cannot size for you. It is also the branch whose zero-arg
    `super()` resolved outside its own MRO while the two lookups shared an
    `as_sql`, raising TypeError on every fenced query.
    """
    from tests.testapp.models import Roster

    clause = str(Roster.objects.filter(pk__overlay_fenced_in=[1, 2]).query)

    assert "ANY (ARRAY" not in clause, "a literal list has no subquery to fence"
    assert " IN (" in clause


@override_settings(DJANGO_OVERLAY_ARRAY_SUBQUERY_IN="yes please")
def test_a_non_boolean_setting_is_refused():
    inner = WideCustomerU7.objects.filter(city="city42").values("id")
    with pytest.raises(ImproperlyConfigured, match="must be a bool"):
        str(WideOrderU7.objects.filter(customer_id__in=inner).query)


def test_it_is_on_by_default():
    from django_overlay.fields import _array_subquery_in_enabled

    assert _array_subquery_in_enabled() is True
