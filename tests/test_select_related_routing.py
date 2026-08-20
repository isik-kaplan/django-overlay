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
from django.db import connection
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
        joined = [(m.pk, m.roster.title, m.member.name) for m in
                  RosterMembership.objects.select_related("roster", "member").order_by("pk")]
    routed = [(m.pk, m.roster.title, m.member.name) for m in
              RosterMembership.objects.select_related("roster", "member").order_by("pk")]

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
    WideCustomer.objects.create(first_name="f", last_name="l", email="e", city="c",
                                postcode="p", status="active", score=1, region=region)

    rows, count = queries_for(lambda: WideCustomer.objects.select_related("region"))

    assert count == 1
    assert "JOIN" in str(WideCustomer.objects.select_related("region").query)


def test_a_bare_select_related_splits_by_target(membership):
    """No arguments means every forward relation, and this model's are all
    overlay, so all of them route."""
    rows, count = queries_for(lambda: RosterMembership.objects.select_related())

    assert count == 3, "memberships, rosters, members"


def test_select_related_none_still_clears(membership):
    rows, count = queries_for(
        lambda: RosterMembership.objects.select_related("roster").select_related(None)
    )

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
    rows, count = queries_for(
        lambda: RosterMembership.objects.select_related("roster").values("id", "roster_id")
    )

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


def test_select_related_after_union_is_refused(membership):
    combined = RosterMembership.objects.all().union(RosterMembership.objects.all())

    with pytest.raises(OverlayConfigurationError, match="select_related\\(\\) after"):
        combined.select_related("roster")


def test_the_refusals_name_the_alternative(membership):
    with pytest.raises(OverlayConfigurationError, match="second query"):
        RosterMembership.objects.select_related("roster").union(RosterMembership.objects.all())
