"""An OverlayModel used as an M2M `through` table.

Every other `through` model in the suite is a plain `models.Model`. This is the
shape a vendor-asserted relationship needs: the link rows themselves come from
the source table, the tenant can add their own and remove the vendor's, and
`roster.members.all()` therefore crosses three views rather than one.

`RosterMembership` is declared `overridable = False` — a link row is a pair of
ids and a role, so there is nothing in it to edit in place. See
tests/test_overridable.py for what that buys and what it refuses.
"""

import pytest
from django.db import NotSupportedError, connection

from tests.testapp.models import (
    Member,
    MemberUuid7Polyfill,
    Roster,
    RosterMembership,
    RosterMembershipUuid7Polyfill,
    RosterUuid7Polyfill,
)
from tests.testapp_shared.models import (
    MemberSource,
    MemberUuid7PolyfillSource,
    RosterMembershipSource,
    RosterMembershipUuid7PolyfillSource,
    RosterSource,
    RosterUuid7PolyfillSource,
)


pytestmark = pytest.mark.django_db


def view_id(source_row) -> int:
    """What a NEGATIVE_ID source row's id looks like through the view."""
    return -source_row.id


@pytest.fixture
def vendor_membership():
    """A roster, a member, and the vendor's assertion that one belongs to the
    other — all three living only in source tables.

    The through row's FK columns hold *view* ids, not source ids. The view
    negates the primary key and passes every other column through untouched,
    so a source FK pointing at another NEGATIVE_ID overlay model has to be
    stored already negated or it points at nothing.
    """
    roster = RosterSource.objects.create(title="vendor roster")
    member = MemberSource.objects.create(name="vendor member")
    link = RosterMembershipSource.objects.create(roster_id=view_id(roster), member_id=view_id(member), role="captain")
    return {"roster": roster, "member": member, "link": link}


# ------------------------------------------------------- it resolves at all


def test_the_through_model_is_the_view():
    through = Roster._meta.get_field("members").remote_field.through
    assert through is RosterMembership
    assert through._meta.db_table == "rostermembership_view"
    assert through._meta.managed is False


def test_the_traversal_crosses_three_views():
    sql = str(Roster.objects.filter(members__name="x").query)
    for view in ("roster_view", "rostermembership_view", "member_view"):
        assert view in sql, f"{view} missing from the traversal"


# --------------------------------------------- vendor-asserted memberships


def test_a_source_only_link_is_visible_through_the_m2m(vendor_membership):
    roster = Roster.objects.get(pk=view_id(vendor_membership["roster"]))
    assert [m.name for m in roster.members.all()] == ["vendor member"]


def test_the_reverse_accessor_works(vendor_membership):
    member = Member.objects.get(pk=view_id(vendor_membership["member"]))
    assert [r.title for r in member.rosters.all()] == ["vendor roster"]


def test_filtering_across_the_through_view(vendor_membership):
    assert Roster.objects.filter(members__name="vendor member").count() == 1
    assert Roster.objects.filter(members__name="nobody").count() == 0


def test_the_through_row_carries_its_own_columns(vendor_membership):
    link = RosterMembership.objects.get(pk=view_id(vendor_membership["link"]))
    assert link.role == "captain"
    assert link.roster_id == view_id(vendor_membership["roster"])


# ------------------------------------------------------- the tenant's own


def test_add_creates_an_organic_link():
    roster = Roster.objects.create(title="ours")
    member = Member.objects.create(name="ours")

    roster.members.add(member)

    assert [m.name for m in roster.members.all()] == ["ours"]
    assert RosterMembership.objects.count() == 1


def test_add_across_a_source_backed_pair(vendor_membership):
    """Both ends source-backed, the link organic — the mixed case, and the one
    a tenant adding to vendor data actually hits."""
    roster = Roster.objects.get(pk=view_id(vendor_membership["roster"]))
    extra = MemberSource.objects.create(name="added by us")

    roster.members.add(Member.objects.get(pk=view_id(extra)))

    assert sorted(m.name for m in roster.members.all()) == ["added by us", "vendor member"]


def test_add_with_through_defaults():
    roster = Roster.objects.create(title="ours")
    member = Member.objects.create(name="ours")

    roster.members.add(member, through_defaults={"role": "lead"})

    assert RosterMembership.objects.get().role == "lead"


def test_set_and_clear():
    roster = Roster.objects.create(title="ours")
    first = Member.objects.create(name="first")
    second = Member.objects.create(name="second")

    roster.members.set([first, second])
    assert roster.members.count() == 2

    roster.members.set([second])
    assert [m.name for m in roster.members.all()] == ["second"]

    roster.members.clear()
    assert roster.members.count() == 0


def test_remove_an_organic_link():
    roster = Roster.objects.create(title="ours")
    member = Member.objects.create(name="ours")
    roster.members.add(member)

    roster.members.remove(member)

    assert roster.members.count() == 0


def test_remove_a_vendor_asserted_link(vendor_membership):
    """The tenant can drop a link the vendor asserted. With soft delete that
    writes a tombstone, which the narrowed anti-join still has to honour — if
    it didn't, the source row would come straight back."""
    roster = Roster.objects.get(pk=view_id(vendor_membership["roster"]))
    member = Member.objects.get(pk=view_id(vendor_membership["member"]))

    roster.members.remove(member)

    assert roster.members.count() == 0
    assert RosterMembership.objects.count() == 0, "the source link must stay hidden, not reappear"


def test_removing_one_link_leaves_the_others(vendor_membership):
    roster = Roster.objects.get(pk=view_id(vendor_membership["roster"]))
    ours = Member.objects.create(name="ours")
    roster.members.add(ours)

    roster.members.remove(Member.objects.get(pk=view_id(vendor_membership["member"])))

    assert [m.name for m in roster.members.all()] == ["ours"]


# --------------------------------------------------------------- integrity


def test_the_m2m_never_returns_a_link_twice(vendor_membership):
    """The whole point of the anti-join. A non-overridable through model
    narrows it to tombstones, so this is the assertion that the narrowing is
    still sufficient."""
    roster = Roster.objects.get(pk=view_id(vendor_membership["roster"]))
    for _ in range(2):
        assert roster.members.count() == 1

    ids = list(RosterMembership.objects.values_list("pk", flat=True))
    assert len(ids) == len(set(ids))


def test_prefetch_related_across_the_through_view(vendor_membership):
    rosters = list(Roster.objects.prefetch_related("members"))
    assert [[m.name for m in r.members.all()] for r in rosters] == [["vendor member"]]


def test_the_through_view_has_the_expected_columns():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'rostermembership_view' ORDER BY ordinal_position"
        )
        columns = {row[0] for row in cursor.fetchall()}
    assert columns == {"id", "roster_id", "member_id", "role"}
    assert "_overlay_deleted" not in columns, "the tombstone flag is base-only"


# ------------------------------------------- the same thing under UUID7


@pytest.fixture
def uuid_vendor_membership():
    """The uuid counterpart of `vendor_membership` — and note what is missing:
    no negation anywhere.

    The view rewrites nothing under a uuid strategy, so the vendor's link can
    hold the vendor's own ids and mean what it says. Under NEGATIVE_ID the same
    data resolves to whichever *tenant-created* rows happen to hold those low
    positive ids."""
    roster = RosterUuid7PolyfillSource.objects.create(title="vendor roster")
    member = MemberUuid7PolyfillSource.objects.create(name="vendor member")
    link = RosterMembershipUuid7PolyfillSource.objects.create(roster_id=roster.id, member_id=member.id, role="captain")
    return {"roster": roster, "member": member, "link": link}


def test_uuid_a_vendor_fk_points_at_the_vendor_row(uuid_vendor_membership):
    """The regression test for source-side foreign keys, from the side that works."""
    link = RosterMembershipUuid7Polyfill.objects.get(pk=uuid_vendor_membership["link"].id)

    assert link.roster_id == uuid_vendor_membership["roster"].id
    assert link.roster.title == "vendor roster"
    assert link.member.name == "vendor member"


def test_uuid_a_vendor_fk_is_not_confused_by_the_tenants_own_rows(uuid_vendor_membership):
    """The exact scenario that silently mis-resolves under NEGATIVE_ID: the
    tenant has their own rows, and the vendor's link must ignore them."""
    ours = RosterUuid7Polyfill.objects.create(title="OUR ROSTER -- private")
    RosterUuid7Polyfill.objects.create(title="another of ours")

    link = RosterMembershipUuid7Polyfill.objects.get(pk=uuid_vendor_membership["link"].id)

    assert link.roster.title == "vendor roster"
    assert ours.members.count() == 0, "the tenant's roster gained nothing"


def test_uuid_the_m2m_traversal_works(uuid_vendor_membership):
    roster = RosterUuid7Polyfill.objects.get(pk=uuid_vendor_membership["roster"].id)
    assert [m.name for m in roster.members.all()] == ["vendor member"]
    assert RosterUuid7Polyfill.objects.filter(members__name="vendor member").count() == 1


def test_uuid_add_and_remove(uuid_vendor_membership):
    roster = RosterUuid7Polyfill.objects.get(pk=uuid_vendor_membership["roster"].id)
    ours = MemberUuid7Polyfill.objects.create(name="ours")

    roster.members.add(ours, through_defaults={"role": "lead"})
    assert sorted(m.name for m in roster.members.all()) == ["ours", "vendor member"]

    roster.members.remove(MemberUuid7Polyfill.objects.get(pk=uuid_vendor_membership["member"].id))
    assert [m.name for m in roster.members.all()] == ["ours"]


def test_uuid_the_through_model_is_non_overridable(uuid_vendor_membership):
    with pytest.raises(NotSupportedError, match="overridable = False"):
        RosterMembershipUuid7Polyfill.objects.update(role="deputy")


def test_uuid_the_anti_join_is_narrowed_to_tombstones():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_viewdef(%s, true)", ["rostermembership_uuid7polyfill_view"])
        definition = cursor.fetchone()[0]
    _, _, subquery = definition.partition("EXISTS")
    assert subquery, "soft delete still needs tombstones to shadow"
    assert "_overlay_deleted" in subquery
