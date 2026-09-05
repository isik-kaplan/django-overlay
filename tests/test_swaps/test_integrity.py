"""The constraint triggers' predicates, run backwards.

Half of these are the collisions and dangling references a swap creates, and
half are the narrowings that have to stay narrow: a base row overriding its
own source row is not a collision with itself, and a value a tombstone
released is not a collision at all. Both halves matter equally -- a check that
blocked on those would block every real swap.
"""

import pytest

from django_overlay.swaps import (
    ERROR,
    WARNING,
    verify_source_swap,
)
from tests.test_swaps.support import (
    analyze,
    assert_finding,
    assert_message,
    codes,
    green,
)
from tests.testapp.models import (
    Person,
    PersonProfile,
    SoftDeleteTest,
    SoftDeleteTestNote,
    SoftDeleteUniqueTest,
    UniqueTest,
)
from tests.testapp_shared.models import PersonSource, SoftDeleteTestSource, UniqueTestSource


pytestmark = pytest.mark.django_db


def test_a_candidate_holding_a_value_a_base_row_already_holds_blocks_the_swap(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    UniqueTest.objects.create(ssn="222-22-2222")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, %s, '')", ["222-22-2222"])
    analyze(db_cursor, "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_message(
        report,
        "S009",
        ERROR,
        "uniquetest_ssn_unique: 1 row(s) in public.green_uniquetest hold a ['ssn'] that a base "
        "row already holds. The constraint would be violated the moment the view reads both, and "
        "no index or trigger raises for it.",
    )


def test_a_candidate_that_duplicates_a_constrained_value_within_itself_blocks_the_swap(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, '333-33-3333', '')")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9002, '333-33-3333', '')")
    analyze(db_cursor, "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_finding(report, "S008", ERROR, "uniquetest_ssn_unique", "more than once within")


def test_a_reference_the_candidate_would_strand_blocks_the_swap(db_cursor):
    """Two dangling references, one of which already dangled before anyone
    proposed a swap. The count in the finding is the one the swap is
    responsible for, because that is the number an operator is deciding on --
    the total is in the parenthesis beside it, not instead of it."""
    PersonSource.objects.create(first_name="Anchor")
    already = PersonSource.objects.create(first_name="Gone")
    stranded = PersonSource.objects.create(first_name="Jane")
    old_profile = PersonProfile.objects.create(person_id=-already.id, bio="")
    PersonProfile.objects.create(person_id=-stranded.id, bio="")
    # The vendor removed this one months ago; the swap neither caused it nor
    # can fix it.
    db_cursor.execute("DELETE FROM testapp_shared_personsource WHERE id = %s", [already.id])
    candidate = green(db_cursor, "testapp_shared_personsource", "green_person")
    db_cursor.execute("DELETE FROM green_person WHERE id = %s", [stranded.id])

    report = verify_source_swap(Person, candidate, identity_columns=["first_name"])

    assert_message(
        report,
        "S007",
        ERROR,
        "1 reference(s) in testapp_personprofile.person_id point at a row the candidate does "
        "not make visible (1 of 2 already dangle today).",
    )

    # Same reason as the warning case below: the reference to the row the
    # vendor deleted has a constraint-trigger event queued against a parent
    # that is legitimately gone, and it fires when this transaction ends.
    old_profile.delete()


def test_a_reference_that_already_dangles_is_not_blamed_on_the_swap(db_cursor):
    # Nothing has ever stopped the vendor deleting a referenced row, so a
    # reference can be dangling before the swap is even proposed. Reporting
    # that as something the candidate caused would block a cutover on a problem
    # the cutover neither creates nor can fix.
    PersonSource.objects.create(first_name="Anchor")
    stranded = PersonSource.objects.create(first_name="Jane")
    profile = PersonProfile.objects.create(person_id=-stranded.id, bio="")
    # The vendor deletes the row out from under the reference. Nothing here can
    # stop that -- we do not own that table and cannot put a trigger on it --
    # so a dangling reference can predate any swap by months.
    db_cursor.execute("DELETE FROM testapp_shared_personsource WHERE id = %s", [stranded.id])
    candidate = green(db_cursor, "testapp_shared_personsource", "green_person")

    report = verify_source_swap(Person, candidate, identity_columns=["first_name"])

    assert_message(
        report,
        "S007",
        WARNING,
        "1 reference(s) in testapp_personprofile.person_id already dangle today and still would. "
        "The swap does not cause this.",
    )

    # The reference's own constraint-trigger event is still queued against a
    # parent that is now legitimately gone, and would fire when this test's
    # transaction ends. Removing the reference is exactly the create-then-delete
    # case the trigger's first guard exists for.
    profile.delete()


def test_a_duplicate_the_current_source_already_has_is_not_blamed_on_the_swap(db_cursor):
    UniqueTestSource.objects.create(ssn="shared")
    UniqueTestSource.objects.create(ssn="shared")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_message(
        report,
        "S008",
        WARNING,
        "uniquetest_ssn_unique: 1 ['ssn'] value(s) already appear more than once within the "
        "current source and still would. Nothing in this package has ever enforced uniqueness "
        "within the source itself.",
    )


def test_a_base_row_overriding_its_own_source_row_is_not_a_collision(db_cursor):
    """A materialised row holds the same value as the source row it shadows, by
    construction -- materialisation copies the whole row. The collision probe
    has to recognise that pairing as one entity and not two, which under
    NEGATIVE_ID means matching a base pk of -5 against a source id of 5. Get the
    sign wrong and every materialised row in the tenant is reported as a
    collision with itself."""
    row = UniqueTestSource.objects.create(ssn="111-11-1111")
    UniqueTest.objects.filter(pk=-row.id).update(notes="edited")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert "S009" not in codes(report), str(report)


def test_a_value_a_tombstone_released_is_not_a_collision(db_cursor):
    """The state the source-side uniqueness trigger deliberately permits: a
    source row is tombstoned, so its value is free, and the tenant takes it for
    a row of their own. Nothing is wrong here and the swap must not say there
    is -- but only the soft-delete narrowing in the probe knows that, and
    without it every released value comes back as a blocking error."""
    released = UniqueTestSource.objects.create(ssn="111-11-1111")
    UniqueTest.objects.filter(pk=-released.id).delete()
    UniqueTest.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert "S009" not in codes(report), str(report)


def test_a_reference_to_a_row_a_tombstone_hides_is_dangling(db_cursor):
    """A reference is dangling when the view cannot return the row, which is not
    the same as the row being gone: a tombstone leaves the base row in place and
    the source row behind it, and hides both. A probe that only asks whether a
    row exists finds one and reports nothing."""
    row = SoftDeleteTestSource.objects.create(first_name="Jane")
    note = SoftDeleteTestNote.objects.create(target_id=-row.id, text="")
    # Written straight to the base table, because the package's own triggers
    # refuse to create this state -- the delete side fires on UPDATE as well as
    # DELETE precisely so a soft delete cannot strand a reference. It is still
    # reachable, by every write that does not go through them: raw SQL, another
    # service on the same database, a reference that predates the constraint.
    db_cursor.execute(
        "INSERT INTO softdeletetest (id, first_name, _overlay_deleted) VALUES (%s, %s, TRUE)",
        [-row.id, "Jane"],
    )
    candidate = green(db_cursor, "testapp_shared_softdeletetestsource", "green_softdeletetest")

    report = verify_source_swap(SoftDeleteTest, candidate, identity_columns=["first_name"])

    assert_finding(report, "S007", WARNING, "testapp_softdeletetestnote.target_id", "already dangle today")

    note.delete()
    db_cursor.execute("DELETE FROM softdeletetest WHERE id = %s", [-row.id])


def test_a_constraint_after_a_clean_one_is_still_checked(db_cursor):
    """Three constraints on one model, and only the last of them is broken. A
    loop that stops at the first constraint it finds nothing wrong with reports
    nothing on any model whose first constraint happens to be fine, which is
    most of them."""
    db_cursor.execute(
        "INSERT INTO testapp_shared_softdeleteuniquetestsource (id, ssn, email, first_name, last_name) "
        "VALUES (1, 's-one', 'one@example.com', 'A', 'One'), (2, 's-two', 'two@example.com', 'B', 'Two')"
    )
    analyze(db_cursor, "testapp_shared_softdeleteuniquetestsource")
    candidate = green(db_cursor, "testapp_shared_softdeleteuniquetestsource", "green_softdeleteunique")
    db_cursor.execute("UPDATE green_softdeleteunique SET email = 'one@example.com' WHERE id = 2")
    analyze(db_cursor, "green_softdeleteunique")

    report = verify_source_swap(SoftDeleteUniqueTest, candidate, identity_columns=["ssn"])

    assert_finding(report, "S008", ERROR, "softdeleteuniquetest_email_uniq", "more than once within")
