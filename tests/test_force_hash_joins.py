"""Banning nested loops when a query crosses more than one m2m hop.

The estimate for two joined overlay views cannot be repaired — measured at
266,974,515,000 rows against 132 actual — but it does not have to be. It is
only fatal because it selects a nested loop that runs to exhaustion. Forbid
that plan and the same estimate is harmless: >20s becomes 457ms.

What matters in these tests is that it fires where it should, stays out of the
way where it should not, leaves the session as it found it, and never changes a
row. The performance itself is measured in tests/probe_plan_forcing.py.
"""

from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from django_overlay.models import (
    _HASH_JOIN_THRESHOLD,
    _HASH_JOIN_THRESHOLD_LIMITED,
    _MAX_SUBQUERY_DEPTH,
    _nested_queries,
    _overlay_views_joined,
    _overlay_views_read,
    planning,
)
from tests.testapp.models import (
    BenchPerson,
    BenchPersonAddress,
    Member,
    PlainPerson,
    Roster,
)
from tests.testapp_shared.models import MemberSource, RosterSource


pytestmark = pytest.mark.django_db

OFF = override_settings(DJANGO_OVERLAY_FORCE_HASH_JOINS=False)

ONE_HOP = {"addresses__city": "city0"}
TWO_HOPS = {"addresses__city": "city0", "phones__kind": "mobile"}
# One hop reached from the through model, which is four views rather than three.
FOUR_VIEWS = {"person__phones__number": "+447000000042"}
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
    # The whole message, not just the exception type. `pytest.raises` alone
    # passes for a message of None, which is what a mutant replaced this one
    # with -- and the message is the only thing that tells a reader which
    # setting they got wrong and what it wanted.
    with override_settings(DJANGO_OVERLAY_FORCE_HASH_JOINS="yes please"):
        with pytest.raises(ImproperlyConfigured) as raised:
            list(BenchPerson.objects.filter(**TWO_HOPS))

    assert str(raised.value) == ("settings.DJANGO_OVERLAY_FORCE_HASH_JOINS must be a bool, got 'yes please'.")


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


def test_the_forced_path_forwards_its_arguments_too():
    """get_aggregation is overridden twice over: once to pass straight through,
    and once inside the forced-hash-join block. They forward separately, and a
    test of the first says nothing about the second.
    """
    query = BenchPerson.objects.filter(**TWO_HOPS).query.clone()
    assert query._wants_hash_joins(), "precondition: this is the forced path"

    result = query.get_aggregation("default", aggregate_exprs={"n": Count("id")})

    assert set(result) == {"n"}


def test_the_threshold_counts_one_hop_as_three_views_and_two_as_five():
    """The constant is only defensible if the counting behind it is right."""
    one_hop = Roster.objects.filter(members__name="m").query
    two_hops = BenchPerson.objects.filter(**TWO_HOPS).query
    assert _overlay_views_joined(one_hop) == 3
    assert _overlay_views_joined(two_hops) == 5
    assert _HASH_JOIN_THRESHOLD_LIMITED <= 3 < _HASH_JOIN_THRESHOLD <= 5


def test_four_views_from_one_hop_is_not_banned():
    """The shape that decided the unsliced threshold, and the regression guard
    on it.

    Views are counted distinct, so an m2m hop steps 3 -> 5 -> 7 and never lands
    on 4 -- which is why the threshold sat at 4 for a while on the reasoning
    that nothing could tell 4 and 5 apart. Starting from the *through* model and
    traversing out of it does land on 4: person_address, person, person_phone,
    phone. That is still one hop, with an extra base view along for the ride.

    Measured at 1,000,000 people on a 45-row scope, the ban made it 3ms -> 36ms
    -- the same regression that ruled out a single threshold of 2, one view
    further along. Lower the threshold back to 4 and this fails.
    """
    four = BenchPersonAddress.objects.filter(**FOUR_VIEWS)
    assert _overlay_views_joined(four.query) == 4, "the shape must still count four"
    assert not any(BAN in statement for statement in statements(four))


def test_the_view_count_is_a_proxy_for_hops_and_four_is_where_it_leaks():
    """Why the number is 5 and not 4, as an assertion rather than a comment.

    The count stands in for how many joins the planner has to size with no
    statistics. One hop is three views and needs no ban; two hops is five and
    does. Four is reachable with one hop, so a threshold of 4 would ban a
    one-hop query -- which is exactly what it did.
    """
    one_hop = BenchPerson.objects.filter(**ONE_HOP).query
    one_hop_from_through = BenchPersonAddress.objects.filter(**FOUR_VIEWS).query
    two_hops = BenchPerson.objects.filter(**TWO_HOPS).query

    assert _overlay_views_joined(one_hop) == 3
    assert _overlay_views_joined(one_hop_from_through) == 4
    assert _overlay_views_joined(two_hops) == 5
    # The threshold has to sit above every one-hop shape and at or below two.
    assert _overlay_views_joined(one_hop_from_through) < _HASH_JOIN_THRESHOLD
    assert _HASH_JOIN_THRESHOLD <= _overlay_views_joined(two_hops)


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


def models_of(query):
    """Which models the queries one level inside `query` belong to."""
    return sorted(inner.model.__name__ for inner in _nested_queries(query))


# _nested_queries is what makes the count see past the outer query, and it was
# reached only through queries whose answer did not depend on it -- twenty-six
# mutants lived in it, renaming every attribute it reads and nulling every
# object it reads them from. Each shape below is a different way a Query can
# hold another one, asserted on the walk directly.
def test_a_subquery_on_the_right_of_a_lookup_is_found():
    scope = Member.objects.filter(name="m").values("pk")
    assert models_of(Roster.objects.filter(pk__in=scope).query) == ["Member"]


def test_a_subquery_on_the_left_of_a_lookup_is_found():
    """`filter(Exists(...))` does not put the expression where you would
    guess: Django compares it to True, so the node's *lhs* is the Exists and
    its rhs is a bool. A walk that read rhs alone found nothing here, and
    nothing in the suite could tell -- the Exists still executes correctly,
    it just stops counting towards the ban."""
    outer = Roster.objects.filter(Exists(Member.objects.filter(name=OuterRef("title"))))
    node = list(outer.query.where.children)[0]
    assert isinstance(node.lhs, Exists) and node.rhs is True, "the shape this test rests on"
    assert models_of(outer.query) == ["Member"]


def test_a_subquery_wrapped_in_an_expression_is_unwrapped():
    """The `.query` hop, which only matters for a wrapped inner query: a bare
    Query on the rhs is already the thing wanted, so a walk that never
    unwrapped anything still passed on `pk__in`."""
    wrapped = Roster.objects.filter(Exists(Member.objects.filter(name=OuterRef("title"))))
    node = list(wrapped.query.where.children)[0]
    assert not hasattr(node.lhs, "alias_map"), "the Exists itself must not look like a Query"
    assert hasattr(node.lhs.query, "alias_map"), "its .query must be the one found"
    assert models_of(wrapped.query) == ["Member"]


def test_the_walk_descends_into_a_compound_where_node():
    """`Q(...) | Q(...)` nests the lookups one level down inside a WhereNode,
    so a walk that treats every node as a leaf sees an empty query -- and both
    branches have to survive it, since abandoning the loop at the first
    compound node loses everything after it."""
    one = Member.objects.filter(name="a").values("pk")
    two = Member.objects.filter(name="b").values("pk")
    single = Roster.objects.filter(Q(title="t") | Q(pk__in=one))
    both = Roster.objects.filter(Q(pk__in=one) | Q(pk__in=two))

    assert list(single.query.where.children)[0].children, "the branch must be nested to prove anything"
    assert models_of(single.query) == ["Member"]
    assert models_of(both.query) == ["Member", "Member"]


def test_an_annotated_subquery_is_found_whichever_form_it_takes():
    """Two shapes that look the same in Django and are not.

    `Exists` keeps its inner query on `.query`. A `Subquery` does not survive
    resolution as a Subquery at all -- what lands in `query.annotations` is the
    inner Query itself, which has no `.query` of its own. Looking only for
    `.query` therefore found the Exists and silently dropped the Subquery, so
    an annotated scope over overlay views did not count towards the ban.
    """
    exists = Roster.objects.annotate(e=Exists(Member.objects.filter(name=OuterRef("title"))))
    subquery = Roster.objects.annotate(n=Subquery(Member.objects.filter(name=OuterRef("title")).values("id")[:1]))
    assert models_of(exists.query) == ["Member"]
    assert models_of(subquery.query) == ["Member"]
    assert _overlay_views_joined(exists.query) == _overlay_views_joined(subquery.query) == 2

    # An aggregate is an annotation too, and must not be mistaken for a
    # subquery -- it has no inner query, and its joins are already in the
    # outer alias map.
    assert models_of(Roster.objects.annotate(n=Count("members")).query) == []


def test_an_annotated_scope_over_two_hops_is_banned_like_a_filtered_one():
    """What the blind spot above cost, at the level that matters: the same
    two-hop scope banned nested loops as a filter and did not as an
    annotation."""
    scope = BenchPerson.objects.filter(**TWO_HOPS).values("pk")[:1]
    annotated = BenchPerson.objects.annotate(scoped=Subquery(scope))
    assert _overlay_views_joined(annotated.query) >= _HASH_JOIN_THRESHOLD
    assert any(BAN in statement for statement in statements(annotated))


def _chain(depth):
    """`depth` levels of `pk__in` nesting, with a view at the bottom that
    appears nowhere else — so whether the walk reached that level is a
    yes-or-no question about one table name."""
    inner = Member.objects.values("pk")
    for _ in range(depth):
        inner = Roster.objects.filter(pk__in=inner).values("pk")
    return inner.query


def test_the_last_level_the_walk_reaches_is_the_limit_itself():
    """Which level the guard cuts at, pinned from both sides.

    _MAX_SUBQUERY_DEPTH levels down is still walked; one more is not. Asserting
    only that a deep chain "still answers" left the boundary free to move by a
    level in either direction, and left the counter free to stop counting --
    four mutants lived in this guard and the recursion that feeds it, moving
    the start, the comparison, and the step.
    """
    marker = Member._meta.db_table
    assert marker in _overlay_views_read(_chain(_MAX_SUBQUERY_DEPTH))
    assert marker not in _overlay_views_read(_chain(_MAX_SUBQUERY_DEPTH + 1))
    # Not vacuous in either direction: the outer view is found at both depths,
    # so the difference above is the guard firing and not the walk failing.
    assert Roster._meta.db_table in _overlay_views_read(_chain(_MAX_SUBQUERY_DEPTH + 1))


def test_lowering_the_threshold_from_outside_is_honoured():
    """The knob benchmark/suites/ban.py turns, asserted here because nothing
    else could catch it going dead.

    `_wants_hash_joins` reads the threshold through the `planning` module rather
    than through a name imported into its own. That distinction is invisible
    until something overrides it from outside: splitting models.py into a
    package moved the binding out from under that benchmark, whose override
    silently stopped doing anything -- it kept passing, and reported the ban as
    costing nothing, because both arms of the comparison were the unbanned one.
    """

    # A fresh queryset per call: `statements()` evaluates it, and an evaluated
    # queryset answers the next call from its own cache without issuing SQL --
    # which looks exactly like "the ban did not fire".
    def one_view():
        return BenchPerson.objects.filter(city="city0")

    assert _overlay_views_joined(one_view().query) == 1
    assert not any(BAN in statement for statement in statements(one_view())), "unbanned to begin with"

    with mock.patch.object(planning, "_HASH_JOIN_THRESHOLD", 1):
        assert any(BAN in statement for statement in statements(one_view())), (
            "the override was not seen by the code that reads it"
        )

    assert not any(BAN in statement for statement in statements(one_view())), "and it is put back"


def test_the_lowered_threshold_fires_on_the_threshold_itself():
    """Two views and a slice is the smallest banned shape, and it has to be
    banned *at* the limit rather than past it -- a `>` here would leave the
    lowered threshold unreachable, since two is as low as a join can go."""
    sliced = Roster.objects.filter(pk__in=Member.objects.values("pk")).order_by("id")[:5]
    assert _overlay_views_joined(sliced.query) == _HASH_JOIN_THRESHOLD_LIMITED == 2
    assert any(BAN in statement for statement in statements(sliced))


def test_a_deeply_chained_query_still_answers():
    """The realistic version of the shape the guard is there for."""
    inner = BenchPerson.objects.filter(city="city0")
    for _ in range(_MAX_SUBQUERY_DEPTH + 4):
        inner = BenchPerson.objects.filter(pk__in=inner.values("pk"))
    assert _overlay_views_joined(inner.query) >= 1
    assert list(inner[:1]) == []
