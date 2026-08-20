"""Banning nested loops when a query crosses more than one m2m hop.

The estimate for two joined overlay views cannot be repaired — measured at
266,974,515,000 rows against 132 actual — but it does not have to be. It is
only fatal because it selects a nested loop that runs to exhaustion. Forbid
that plan and the same estimate is harmless: >20s becomes 457ms.

What matters in these tests is that it fires where it should, stays out of the
way where it should not, leaves the session as it found it, and never changes a
row. The performance itself is measured in tests/probe_plan_forcing.py.
"""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.models import Count
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from django_overlay.models import (
    _HASH_JOIN_THRESHOLD,
    _HASH_JOIN_THRESHOLD_LIMITED,
    _MAX_SUBQUERY_DEPTH,
    _overlay_views_joined,
    _overlay_views_read,
)
from tests.testapp.models import BenchPerson, Member, PlainPerson, Roster
from tests.testapp_shared.models import MemberSource, RosterSource


pytestmark = pytest.mark.django_db

OFF = override_settings(DJANGO_OVERLAY_FORCE_HASH_JOINS=False)

TWO_HOPS = {"addresses__city": "city0", "phones__kind": "mobile"}
BAN = "SET enable_nestloop = off"


def statements(queryset):
    with CaptureQueriesContext(connection) as captured:
        list(queryset)
    return [entry["sql"] for entry in captured.captured_queries]


def test_two_m2m_hops_ban_nested_loops():
    assert any(BAN in statement for statement in statements(BenchPerson.objects.filter(**TWO_HOPS)))


def test_one_hop_with_a_limit_bans_nested_loops():
    """A LIMIT collapses the tuple fraction, so one join is enough to go wrong.

    Measured 7,854ms unbanned against 581ms banned, in both passes.
    """
    paged = Roster.objects.filter(members__name="m").order_by("id")[:20]
    assert any(BAN in statement for statement in statements(paged))


def test_one_hop_without_a_limit_is_left_alone():
    """The regression the second threshold exists to prevent.

    Banning nested loops on a selective filter with no LIMIT measured 7ms ->
    60ms: 200 rows is exactly where a nested loop is the right plan, and
    forbidding it buys a hash build over a large relation for nothing.
    """
    assert not any(BAN in statement for statement in statements(Roster.objects.filter(members__name="m")))


def test_a_query_touching_one_view_is_left_alone():
    """No second relation, no nested loop to ban, no reason for two round
    trips. This is what the threshold is still for."""
    single = BenchPerson.objects.filter(city__in=["city0", "city1"])
    assert _overlay_views_joined(single.query) == 1
    assert not any(BAN in statement for statement in statements(single))


def test_a_plain_model_is_left_alone():
    assert not any(BAN in statement for statement in statements(PlainPerson.objects.filter(**TWO_HOPS)))


def test_the_setting_turns_it_off():
    with OFF:
        assert not any(BAN in statement for statement in statements(BenchPerson.objects.filter(**TWO_HOPS)))


def test_a_non_bool_setting_is_refused():
    with override_settings(DJANGO_OVERLAY_FORCE_HASH_JOINS="yes please"), pytest.raises(ImproperlyConfigured):
        list(BenchPerson.objects.filter(**TWO_HOPS))


def test_the_session_setting_is_restored():
    """The ban must not outlive the statement.

    `SET`, not `SET LOCAL`: LOCAL reverts at the end of the transaction, which
    inside a caller's atomic block would leave nested loops banned for every
    later statement in it.
    """
    list(BenchPerson.objects.filter(**TWO_HOPS))
    with connection.cursor() as cursor:
        cursor.execute("SHOW enable_nestloop")
        assert cursor.fetchone()[0] == "on"


def test_a_caller_who_banned_them_first_keeps_their_setting():
    """The previous value is read back, not assumed to be `on`."""
    with connection.cursor() as cursor:
        cursor.execute("SET enable_nestloop = off")
    try:
        list(BenchPerson.objects.filter(**TWO_HOPS))
        with connection.cursor() as cursor:
            cursor.execute("SHOW enable_nestloop")
            assert cursor.fetchone()[0] == "off"
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET enable_nestloop = on")


def test_the_rows_are_identical_either_way():
    """The whole justification for doing this silently. A plan-level change
    cannot alter results, and this asserts it rather than assuming it —
    including the row multiplicity an m2m join produces, which is what the
    fence had to be so careful about."""
    roster_source = RosterSource.objects.create(title="r")
    roster = Roster.objects.get(pk=-roster_source.id)
    for _ in range(3):
        member_source = MemberSource.objects.create(name="shared")
        roster.members.add(Member.objects.get(pk=-member_source.id))

    def titles():
        return sorted(Roster.objects.filter(members__name="shared").values_list("title", flat=True))

    with OFF:
        unbanned = titles()
    assert len(unbanned) == 3, "the join should still multiply rows"
    assert titles() == unbanned


def test_views_inside_a_subquery_are_counted():
    """A scope attached as `pk__in=<queryset>` hides its joins from the outer
    alias map, but Postgres plans one statement containing all of them.

    Counting only the outer query saw one view here, declined to ban anything,
    and let a summary over a two-hop scope run past four minutes while the same
    scope resolved on its own in 608ms.
    """
    scope = BenchPerson.objects.filter(**TWO_HOPS).values("pk")
    outer = BenchPerson.objects.filter(pk__in=scope)

    assert _overlay_views_joined(outer.query) >= _HASH_JOIN_THRESHOLD
    assert any(BAN in statement for statement in statements(outer))


def test_a_subquery_over_plain_tables_does_not_trip_it():
    """The count must be of overlay views, not of subqueries."""
    scope = PlainPerson.objects.filter(**TWO_HOPS).values("pk")
    assert not any(BAN in statement for statement in statements(PlainPerson.objects.filter(pk__in=scope)))


def test_count_over_a_distinct_query_is_covered():
    """`.values("pk").distinct().count()` is how you count a saved search's
    matches, and it does not reach `get_compiler()`.

    Django wraps a distinct query in an outer sql.AggregateQuery -- a plain
    Query, not OverlayQuery -- and compiles that instead, so hooking only
    get_compiler() skipped the shape most likely to need the ban. It ran past
    30s while values_list() over the same scope returned in 608ms.
    """
    counted = BenchPerson.objects.filter(**TWO_HOPS).values("pk").distinct()
    with CaptureQueriesContext(connection) as captured:
        counted.count()
    assert any(BAN in entry["sql"] for entry in captured.captured_queries)


def test_aggregate_is_covered():
    with CaptureQueriesContext(connection) as captured:
        BenchPerson.objects.filter(**TWO_HOPS).aggregate(n=Count("id", distinct=True))
    assert any(BAN in entry["sql"] for entry in captured.captured_queries)


def test_the_threshold_counts_one_hop_as_three_views_and_two_as_five():
    """The constant is only defensible if the counting behind it is right."""
    one_hop = Roster.objects.filter(members__name="m").query
    two_hops = BenchPerson.objects.filter(**TWO_HOPS).query
    assert _overlay_views_joined(one_hop) == 3
    assert _overlay_views_joined(two_hops) == 5
    assert _HASH_JOIN_THRESHOLD_LIMITED <= 3 < _HASH_JOIN_THRESHOLD <= 5


def test_a_limit_lowers_the_bar_rather_than_removing_it():
    """One view plus a LIMIT is still one relation: nothing to nested-loop
    against, so the lowered threshold must not fire on it either."""
    single = BenchPerson.objects.filter(city__in=["city0"]).order_by("id")[:20]
    assert _overlay_views_joined(single.query) == 1
    assert not any(BAN in statement for statement in statements(single))


def test_the_fences_own_subquery_does_not_inflate_the_count():
    """The m2m fence adds a subquery over the through and target views that the
    outer query already joins. Counting occurrences rather than distinct tables
    scored a single hop at seven and banned nested loops on the shape the
    threshold exists to protect."""
    one_hop = Roster.objects.filter(members__name="m")
    assert "= ANY (ARRAY" in str(one_hop.query), "the fence must be present for this to prove anything"
    assert _overlay_views_joined(one_hop.query) == 3


def test_the_walk_stops_before_it_can_recurse_forever():
    """The depth guard, exercised directly.

    Counting views means descending into every nested query, and a Query can
    hold a reference that leads back to itself -- a combined queryset reused
    inside its own filter, a lookup whose rhs is the queryset being built. The
    guard is what stops that becoming a RecursionError inside get_compiler(),
    where the traceback would point at Django rather than at this walk.

    Nothing in the suite nests ten deep, and nothing should have to, so this
    calls the walk at the limit rather than building a pathological query to
    reach it."""
    query = BenchPerson.objects.filter(**TWO_HOPS).query
    assert _overlay_views_read(query) != set(), "the query must have views to find"
    assert _overlay_views_read(query, _depth=_MAX_SUBQUERY_DEPTH + 1) == set()


def test_a_deeply_chained_query_still_answers():
    """The realistic version of the shape the guard is there for."""
    inner = BenchPerson.objects.filter(city="city0")
    for _ in range(_MAX_SUBQUERY_DEPTH + 4):
        inner = BenchPerson.objects.filter(pk__in=inner.values("pk"))
    assert _overlay_views_joined(inner.query) >= 1
    assert list(inner[:1]) == []
