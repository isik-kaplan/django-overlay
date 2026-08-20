"""`OverlayMeta.overridable = False`, per TODO/01.

A model that can never have a source row edited in place doesn't need the
view's anti-join to stop a materialised row appearing twice — nothing is ever
materialised. Dropping it is the largest measured opportunity in the project:
the same two tables ordered by id measured 0.031ms as a bare `UNION ALL`
against 53ms with the anti-join above them, because `Merge Append` lets `LIMIT`
stop early where `Append` over a `Hash Anti Join` cannot.

The view is only allowed to make that assumption because the triggers enforce
it. Two holes have to be shut, and both are tested here:

  * an UPDATE that would copy a source row down into the base table;
  * an INSERT that hands over a primary key belonging to an existing source
    row, which would put the same id in both branches.

Get either wrong and the view silently returns a row twice, which is the worst
failure mode available here — hence a hard error rather than a warning.
"""

import pytest
from django.db import NotSupportedError, connection, transaction
from django.db.models import Q

from django_overlay.exceptions import OverlayConfigurationError
from django_overlay.sql import anti_join_kind
from tests.testapp.models import AuditEntry, Member, Person, Roster, RosterMembership
from tests.testapp_shared.models import (
    AuditEntrySource,
    MemberSource,
    PersonSource,
    RosterMembershipSource,
    RosterSource,
)


pytestmark = pytest.mark.django_db


def viewdef(view_name: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_viewdef(%s, true)", [view_name])
        return cursor.fetchone()[0]


def view_count(model) -> int:
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT count(*) FROM "{model._meta.db_table}"')  # noqa: S608 - identifier from _meta
        return cursor.fetchone()[0]


# ------------------------------------------------------- which anti-join


def test_anti_join_kind_covers_the_three_cases():
    assert anti_join_kind(overridable=True, soft_delete=True) == "full"
    assert anti_join_kind(overridable=True, soft_delete=False) == "full"
    assert anti_join_kind(overridable=False, soft_delete=True) == "tombstones"
    assert anti_join_kind(overridable=False, soft_delete=False) is None


def anti_join_subquery(view_name: str) -> str | None:
    """The text of the view's anti-join, or None if it hasn't got one.

    Matched on `EXISTS` rather than `NOT EXISTS` because pg_get_viewdef
    normalises the latter to `NOT (EXISTS (...))`."""
    definition = viewdef(view_name)
    _, _, after = definition.partition("EXISTS")
    return after or None


def test_an_overridable_model_keeps_the_full_anti_join():
    subquery = anti_join_subquery("person_view")
    assert subquery is not None
    assert "_overlay_deleted" not in subquery, "the full anti-join excludes on any base row, not just tombstones"


def test_a_non_overridable_soft_delete_model_narrows_it_to_tombstones():
    subquery = anti_join_subquery("rostermembership_view")
    assert subquery is not None
    assert "_overlay_deleted" in subquery, "only a tombstone can shadow a source row when nothing is materialised"


def test_a_non_overridable_hard_delete_model_has_no_anti_join():
    assert anti_join_subquery("auditentry_view") is None


# -------------------------------------------------------- updates refused


def test_saving_a_source_backed_row_raises():
    source = AuditEntrySource.objects.create(note="vendor")
    entry = AuditEntry.objects.get(pk=-source.id)
    entry.note = "edited"

    with pytest.raises(NotSupportedError, match="overridable = False"):
        entry.save()


def test_queryset_update_on_a_source_backed_row_raises():
    AuditEntrySource.objects.create(note="vendor")

    with pytest.raises(NotSupportedError, match="overridable = False"):
        AuditEntry.objects.update(note="edited")


def test_the_source_row_is_untouched_after_a_refused_update():
    source = AuditEntrySource.objects.create(note="vendor")
    entry = AuditEntry.objects.get(pk=-source.id)
    entry.note = "edited"

    # The exception comes from the database, so it marks the transaction
    # broken — a savepoint is what makes the assertions below runnable.
    with pytest.raises(NotSupportedError), transaction.atomic():
        entry.save()

    source.refresh_from_db()
    assert source.note == "vendor"
    assert AuditEntry.objects.get(pk=-source.id).note == "vendor"


def test_updating_an_organic_row_still_works():
    """`overridable = False` forbids overriding *vendor* rows. A row the tenant
    created is theirs to edit."""
    entry = AuditEntry.objects.create(note="ours")

    entry.note = "edited"
    entry.save()

    assert AuditEntry.objects.get(pk=entry.pk).note == "edited"


def test_updating_an_organic_through_row_still_works():
    roster = Roster.objects.create(title="ours")
    member = Member.objects.create(name="ours")
    roster.members.add(member, through_defaults={"role": "lead"})

    RosterMembership.objects.update(role="deputy")

    assert RosterMembership.objects.get().role == "deputy"


def test_updating_a_vendor_asserted_through_row_raises():
    roster = RosterSource.objects.create(title="r")
    member = MemberSource.objects.create(name="m")
    RosterMembershipSource.objects.create(roster_id=-roster.id, member_id=-member.id, role="captain")

    with pytest.raises(NotSupportedError, match="overridable = False"):
        RosterMembership.objects.update(role="deputy")


def test_an_overridable_model_still_copies_on_write():
    """The default path has to be untouched — this is the behaviour every
    other test in the suite depends on."""
    source = PersonSource.objects.create(first_name="Src", age=1)
    person = Person.objects.get(pk=-source.id)

    person.age = 99
    person.save()

    assert Person.objects.get(pk=-source.id).age == 99
    assert Person.objects.get(pk=-source.id).first_name == "Src"


# -------------------------------------------------------- inserts guarded


def test_inserting_over_a_source_id_raises():
    """Without this the base row and the source row both appear, and the view
    returns the same id twice with nothing left to notice."""
    source = AuditEntrySource.objects.create(note="vendor")

    with pytest.raises(NotSupportedError, match="id of an existing source row"):
        AuditEntry.objects.create(pk=-source.id, note="collision")


def test_inserting_over_a_source_id_raises_on_a_through_model():
    roster = RosterSource.objects.create(title="r")
    member = MemberSource.objects.create(name="m")
    link = RosterMembershipSource.objects.create(roster_id=-roster.id, member_id=-member.id)

    with pytest.raises(NotSupportedError, match="id of an existing source row"):
        RosterMembership.objects.create(pk=-link.id, roster_id=-roster.id, member_id=-member.id)


def test_an_explicit_pk_that_collides_with_nothing_is_fine():
    AuditEntrySource.objects.create(note="vendor")

    entry = AuditEntry.objects.create(pk=4242, note="ours")

    assert AuditEntry.objects.get(pk=4242).note == "ours"
    assert entry.pk == 4242


def test_a_generated_pk_is_never_checked_against_the_source():
    """The guard only costs a lookup when the caller supplies the pk. A
    sequence-generated id is positive and source ids arrive negated, so they
    cannot collide."""
    AuditEntrySource.objects.create(note="vendor")

    entry = AuditEntry.objects.create(note="ours")

    assert entry.pk > 0


def test_an_overridable_model_allows_an_explicit_colliding_pk():
    """On an overridable model that insert is exactly copy-on-write, and the
    full anti-join makes it safe. The guard must not leak onto it."""
    source = PersonSource.objects.create(first_name="Src", age=1)

    Person.objects.create(pk=-source.id, first_name="Ours", age=2)

    assert Person.objects.get(pk=-source.id).first_name == "Ours"
    assert Person.objects.filter(pk=-source.id).count() == 1


# ------------------------------------------------------------- integrity


def test_no_duplicates_with_the_anti_join_gone():
    """The assertion the dropped anti-join stands on."""
    for i in range(3):
        AuditEntrySource.objects.create(note=f"vendor{i}")
    AuditEntry.objects.create(note="ours")

    assert AuditEntry.objects.count() == view_count(AuditEntry) == 4
    ids = list(AuditEntry.objects.values_list("pk", flat=True))
    assert len(ids) == len(set(ids))


def test_a_tombstone_still_shadows_its_source_row():
    """The narrowed anti-join's whole job."""
    roster = RosterSource.objects.create(title="r")
    member = MemberSource.objects.create(name="m")
    RosterMembershipSource.objects.create(roster_id=-roster.id, member_id=-member.id)

    assert RosterMembership.objects.count() == 1
    RosterMembership.objects.get().delete()

    assert RosterMembership.objects.count() == view_count(RosterMembership) == 0


def test_a_tombstone_does_not_hide_anyone_elses_row():
    roster = RosterSource.objects.create(title="r")
    first = MemberSource.objects.create(name="first")
    second = MemberSource.objects.create(name="second")
    doomed = RosterMembershipSource.objects.create(roster_id=-roster.id, member_id=-first.id)
    RosterMembershipSource.objects.create(roster_id=-roster.id, member_id=-second.id)

    RosterMembership.objects.get(pk=-doomed.id).delete()

    remaining = RosterMembership.objects.all()
    assert [link.member_id for link in remaining] == [-second.id]


def test_hard_delete_of_a_source_row_is_a_no_op():
    """Documented, not desirable. `soft_delete = False` writes no tombstone, so
    there is nothing to shadow the source row and it stays visible — Django
    reports a delete that did not happen. This is why soft delete is the
    default; it is not specific to `overridable`."""
    source = AuditEntrySource.objects.create(note="vendor")

    AuditEntry.objects.get(pk=-source.id).delete()

    assert AuditEntry.objects.filter(pk=-source.id).exists(), "the vendor row is still there"


def test_organic_rows_and_source_rows_coexist():
    source = AuditEntrySource.objects.create(note="vendor")
    ours = AuditEntry.objects.create(note="ours")

    notes = set(AuditEntry.objects.values_list("note", flat=True))
    assert notes == {"vendor", "ours"}
    assert AuditEntry.objects.filter(Q(pk=-source.id) | Q(pk=ours.pk)).count() == 2


# ------------------------------------------------------------ declaration


def test_overridable_must_be_a_bool():
    from django_overlay.models import OverlayMeta as BaseOverlayMeta
    from django_overlay.models import OverlayModel

    with pytest.raises(OverlayConfigurationError, match="overridable must be a bool"):

        class Broken(OverlayModel):
            class Meta:
                app_label = "testapp"

            class OverlayMeta(BaseOverlayMeta):
                table_name = "broken"
                overridable = "no"

                @staticmethod
                def get_source():
                    return None


def test_overridable_defaults_to_true():
    assert Person._overlay_meta.overridable is True
    assert RosterMembership._overlay_meta.overridable is False
