"""select_related() on an overlay path becomes prefetch_related().

A join between two overlay views is a join between two appendrels, and the
planner has statistics for neither: on the production-shaped graph a detail
page joined that way is 15.2ms against a 0.2ms plain-table baseline, and the
same rows fetched as two queries with a literal id list are 0.1ms.

What must stay true is that nothing else changes. `link.roster` is still a
single instance, still `None` for a null FK, and the rows are identical. The
only visible difference is one more query, and that is the point.

The cases the routing refuses rather than silently degrading are at the bottom.
"""

import pytest
from django.db import connection, models
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from django_overlay.exceptions import OverlayConfigurationError
from tests.testapp.models import (
    Member,
    NullableFkOverlay,
    Roster,
    RosterMembership,
    WideCustomer,
    WideRegion,
)
from tests.testapp_shared.models import MemberSource, RosterSource


pytestmark = pytest.mark.django_db(transaction=True)

OFF = override_settings(DJANGO_OVERLAY_REDIRECT_SELECT_RELATED=False)


@pytest.fixture
def membership():
    roster_source = RosterSource.objects.create(title="r")
    member_source = MemberSource.objects.create(name="m")
    roster = Roster.objects.get(pk=-roster_source.id)
    member = Member.objects.get(pk=-member_source.id)
    roster.members.add(member)
    return RosterMembership.objects.get()


def queries_for(build):
    with CaptureQueriesContext(connection) as captured:
        result = list(build())
    return result, len(captured)


# ------------------------------------------------------------- the redirect


def test_an_overlay_path_is_prefetched_not_joined(membership):
    rows, count = queries_for(lambda: RosterMembership.objects.select_related("roster"))

    assert count == 2, "one for the memberships, one for the rosters"
    assert "JOIN" not in str(RosterMembership.objects.select_related("roster").query)


def test_attribute_access_is_unchanged(membership):
    """The whole reason this is safe to do silently."""
    with OFF:
        joined = RosterMembership.objects.select_related("roster").get()
    prefetched = RosterMembership.objects.select_related("roster").get()

    assert isinstance(prefetched.roster, Roster)
    assert prefetched.roster.pk == joined.roster.pk
    assert prefetched.roster.title == joined.roster.title


def test_the_related_object_costs_no_further_query(membership):
    """A prefetch that did not populate the cache would still *work* — it would
    just lazy-load. That would be a silent N+1, so pin it."""
    memberships = list(RosterMembership.objects.select_related("roster", "member"))

    with CaptureQueriesContext(connection) as captured:
        for row in memberships:
            assert row.roster.title
            assert row.member.name
    assert len(captured) == 0


def test_rows_are_identical_either_way(membership):
    with OFF:
        joined = [
            (m.pk, m.roster.title, m.member.name)
            for m in RosterMembership.objects.select_related("roster", "member").order_by("pk")
        ]
    routed = [
        (m.pk, m.roster.title, m.member.name)
        for m in RosterMembership.objects.select_related("roster", "member").order_by("pk")
    ]

    assert routed == joined


def test_a_null_foreign_key_is_still_none():
    NullableFkOverlay.objects.create(label="orphan", person=None)

    row = NullableFkOverlay.objects.select_related("person").get()

    assert row.person is None


# --------------------------------------------------- what must keep joining


def test_a_plain_target_still_joins():
    """WideRegion is an ordinary table. Its statistics rescue the estimate,
    there is no appendrel, and a join is the right plan."""
    region = WideRegion.objects.create(name="region7", country="GB")
    WideCustomer.objects.create(
        first_name="f", last_name="l", email="e", city="c", postcode="p", status="active", score=1, region=region
    )

    rows, count = queries_for(lambda: WideCustomer.objects.select_related("region"))

    assert count == 1
    assert "JOIN" in str(WideCustomer.objects.select_related("region").query)


def test_a_bare_select_related_splits_by_target(membership):
    """No arguments means every forward relation, and this model's are all
    overlay, so all of them route."""
    rows, count = queries_for(lambda: RosterMembership.objects.select_related())

    assert count == 3, "memberships, rosters, members"


def test_select_related_none_still_clears(membership):
    rows, count = queries_for(lambda: RosterMembership.objects.select_related("roster").select_related(None))

    assert count == 1


def test_the_setting_turns_it_off(membership):
    with OFF:
        rows, count = queries_for(lambda: RosterMembership.objects.select_related("roster"))

    assert count == 1
    with OFF:
        assert "JOIN" in str(RosterMembership.objects.select_related("roster").query)


def test_values_drops_the_prefetch_rather_than_failing(membership):
    """Django ignores select_related() under values() anyway, so dropping the
    prefetch is what "unchanged" means here."""
    rows, count = queries_for(lambda: RosterMembership.objects.select_related("roster").values("id", "roster_id"))

    assert count == 1
    assert rows and set(rows[0]) == {"id", "roster_id"}


# ------------------------------------------------------- what it refuses


def test_iterator_without_chunk_size_is_refused(membership):
    with pytest.raises(OverlayConfigurationError, match="iterator\\(\\) without chunk_size"):
        list(RosterMembership.objects.select_related("roster").iterator())


def test_iterator_with_chunk_size_is_allowed(membership):
    rows = list(RosterMembership.objects.select_related("roster").iterator(chunk_size=100))

    assert rows[0].roster.title == "r"


def test_union_after_select_related_is_refused(membership):
    with pytest.raises(OverlayConfigurationError, match="union\\(\\)"):
        RosterMembership.objects.select_related("roster").union(RosterMembership.objects.all())


def test_the_routing_survives_a_chained_queryset(membership):
    """`_clone` carries the record of what was redirected, and that record is
    what every refusal below reads.

    Nothing chained anything onto a redirected queryset before, so dropping the
    record on clone was invisible: the refusals still fired on the queryset
    select_related() returned, and Django's own _clone copies the prefetch
    lookups either way, so the rows came back right too. What is lost is the
    refusal one `.filter()` later -- which is where a real caller would hit it.
    """
    chained = RosterMembership.objects.select_related("roster").filter(roster__title="r")
    assert chained._overlay_redirected == ("roster",)

    with pytest.raises(OverlayConfigurationError, match="union\\(\\)"):
        chained.union(RosterMembership.objects.all())

    # And the prefetch it stood in for still happens, so the clone carried both
    # halves rather than neither.
    rows, queries = queries_for(lambda: chained)
    assert rows[0].roster.title == "r"
    assert queries == 2


def test_select_related_after_union_is_refused(membership):
    combined = RosterMembership.objects.all().union(RosterMembership.objects.all())

    with pytest.raises(OverlayConfigurationError, match="select_related\\(\\) after"):
        combined.select_related("roster")


# All four refusals come out of _refuse_after_redirect, so the shared half of
# the message is asserted once and each caller's two arguments -- the operation
# name and the remedy -- are asserted per method. `match="union\\(\\)"` was true
# of a message with everything after it garbled, which is where a dozen mutants
# were living: the remedy blanked, uppercased, or dropped entirely.
WHY = (
    " cannot be combined with select_related() on an overlay model. select_related() "
    "is routed to prefetch_related() here, because joining two overlay views measured "
    "76x-1304x a plain table. "
)
SECOND_QUERY = "Fetch the related rows in a second query."


def refusal(operation, remedy):
    return f"{operation}{WHY}{remedy}"


def test_the_refusals_name_the_alternative(membership):
    with pytest.raises(OverlayConfigurationError) as raised:
        RosterMembership.objects.select_related("roster").union(RosterMembership.objects.all())

    assert str(raised.value) == refusal("union()", SECOND_QUERY)


@pytest.mark.parametrize("operation", ["union", "intersection", "difference"])
def test_every_set_operation_refuses_with_its_own_name(membership, operation):
    """One helper, three callers, three names -- and a mutant per name."""
    queryset = RosterMembership.objects.select_related("roster")

    with pytest.raises(OverlayConfigurationError) as raised:
        getattr(queryset, operation)(RosterMembership.objects.all())

    assert str(raised.value) == refusal(f"{operation}()", SECOND_QUERY)


def test_the_iterator_refusal_says_what_to_pass_instead(membership):
    with pytest.raises(OverlayConfigurationError) as raised:
        list(RosterMembership.objects.select_related("roster").iterator())

    assert str(raised.value) == refusal(
        "iterator() without chunk_size",
        "Pass a chunk_size, or drop the select_related() and let the prefetch happen on the whole queryset.",
    )


def test_intersection_after_select_related_is_refused(membership):
    """Same reasoning as union(): a set operation over a queryset carrying a
    pending prefetch silently drops the prefetch, so the related attribute
    would be there on one side and missing on the other."""
    with pytest.raises(OverlayConfigurationError, match="intersection\\(\\)"):
        RosterMembership.objects.select_related("roster").intersection(RosterMembership.objects.all())


def test_difference_after_select_related_is_refused(membership):
    with pytest.raises(OverlayConfigurationError, match="difference\\(\\)"):
        RosterMembership.objects.select_related("roster").difference(RosterMembership.objects.all())


def test_the_set_operations_are_untouched_without_a_redirect(membership):
    """The refusal is about the redirect, not about set operations. Without a
    select_related() to reroute, both must behave exactly as Django's do."""
    everything = RosterMembership.objects.all()
    assert list(everything.intersection(RosterMembership.objects.all())) == [membership]
    assert list(everything.difference(RosterMembership.objects.none())) == [membership]


def test_a_nested_path_is_routed_by_its_first_segment(membership):
    """`path.split(LOOKUP_SEP)[0]` -- splitting on the separator, not on
    whitespace.

    Every select_related() in the suite named a single segment, where splitting
    on anything at all returns the whole string and the mutation is invisible.
    A two-segment path is routed by its head, and getting that wrong sends an
    overlay relation down the join half -- the 76x-1304x join this routing
    exists to avoid.
    """
    from tests.testapp.models import WideOrderLine

    queryset = WideOrderLine.objects.select_related("order__customer")
    prefetched, joined = queryset._split_select_related(["order__customer"])

    assert prefetched == ["order__customer"], "routed by its head, which is an overlay relation"
    assert joined == []


def test_clearing_removes_the_prefetch_it_stood_in_for(membership):
    """`select_related(None)` has to undo the routing, not just the joins.

    The existing test asserts the query count is back to one, which is also
    true if the prefetch lookup is left behind and simply never used. What
    matters is that the lookup is gone: left in place it outlives the
    select_related() it replaced and fires on some later evaluation.
    """
    routed = RosterMembership.objects.select_related("roster")
    assert routed._prefetch_related_lookups == ("roster",), "precondition: it was routed"

    cleared = routed.select_related(None)

    assert cleared._prefetch_related_lookups == ()
    assert cleared._overlay_redirected == ()


def test_values_removes_the_prefetch_rather_than_leaving_it_dangling(membership):
    """The same undo under values(), which has its own copy of it."""
    rows = RosterMembership.objects.select_related("roster").values("id", "roster_id")

    assert rows._prefetch_related_lookups == ()
    assert rows._overlay_redirected == ()


def test_the_query_wrappers_forward_keyword_arguments(membership):
    """OverlayQuery overrides two Django internals and forwards *args/**kwargs.

    Django calls both positionally, so dropping **kwargs changed nothing any
    test could see -- but these override an API whose signature this library
    does not control, and a keyword call has to keep working. Called with
    keywords here, which is the only way to observe the forwarding at all.
    """
    from django.db.models import Count

    query = RosterMembership.objects.all().query.clone()

    by_keyword = query.get_aggregation("default", aggregate_exprs={"n": Count("*")})
    by_position = RosterMembership.objects.all().query.clone().get_aggregation("default", {"n": Count("*")})

    assert set(by_keyword) == {"n"}, "the keyword form has to reach super()"
    assert set(by_position) == {"n"}, "and so does the positional one"


def test_values_keeps_its_annotations(membership):
    """`super()._values(*fields, **expressions)` -- the expressions half.

    values() takes keyword expressions as well as field names, and nothing here
    had ever passed one, so dropping them changed nothing any test could see
    while in practice every annotation under values() would vanish.
    """
    rows = list(
        RosterMembership.objects.select_related("roster").values(
            "id", doubled=models.F("roster_id") + models.F("roster_id")
        )
    )

    assert rows and set(rows[0]) == {"id", "doubled"}


def test_an_unknown_path_does_not_hide_the_ones_after_it():
    """The unknown-path branch continues; it does not abandon the list.

    Ordering is the whole test: with the unknown name first, breaking out of
    the loop instead of skipping past it drops every remaining path into
    neither half, and the overlay relation that follows quietly stays a join.
    A single-element list cannot tell the two apart.
    """
    from tests.testapp.models import WideOrderLine

    queryset = WideOrderLine.objects.all()
    prefetched, joined = queryset._split_select_related(["nonexistent", "order"])

    assert prefetched == ["order"], "the overlay relation after the unknown name is still routed"
    assert joined == ["nonexistent"]


def test_a_path_django_does_not_know_is_left_for_django_to_reject(membership):
    """_split_select_related() does not validate paths, it only routes them.

    A name that is not a field cannot be an overlay relation, so it goes in the
    join half untouched and Django raises its own FieldError -- which names the
    available choices. Swallowing it here to raise something of our own would
    be a worse message about a mistake this library did not catch.
    """
    with pytest.raises(Exception, match="nonexistent"):
        list(RosterMembership.objects.select_related("nonexistent"))
