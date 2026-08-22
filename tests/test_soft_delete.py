import pytest
from django.db import IntegrityError, transaction

from tests.testapp.models import SoftDeleteTest, SoftDeleteTestNoSource, SoftDeleteTestNote
from tests.testapp_shared.models import SoftDeleteTestSource


pytestmark = pytest.mark.django_db


def test_delete_hides_a_materialized_row_and_reset_to_source_restores_the_pristine_value():
    source = SoftDeleteTestSource.objects.create(first_name="Original")
    view_id = -source.id
    SoftDeleteTest.objects.filter(id=view_id).update(first_name="Edited")
    assert SoftDeleteTest.objects.get(id=view_id).first_name == "Edited"

    SoftDeleteTest.objects.get(id=view_id).delete()
    assert not SoftDeleteTest.objects.filter(id=view_id).exists()

    SoftDeleteTest(pk=view_id).reset_to_source()
    assert SoftDeleteTest.objects.get(id=view_id).first_name == "Original"


def test_delete_hides_an_untouched_source_row_and_reset_to_source_brings_it_back():
    source = SoftDeleteTestSource.objects.create(first_name="Untouched")
    view_id = -source.id

    SoftDeleteTest.objects.get(id=view_id).delete()
    assert not SoftDeleteTest.objects.filter(id=view_id).exists()

    SoftDeleteTest(pk=view_id).reset_to_source()
    assert SoftDeleteTest.objects.get(id=view_id).first_name == "Untouched"


def test_reset_to_source_without_a_prior_delete_discards_an_edit():
    source = SoftDeleteTestSource.objects.create(first_name="Pristine")
    view_id = -source.id
    SoftDeleteTest.objects.filter(id=view_id).update(first_name="Edited")

    SoftDeleteTest(pk=view_id).reset_to_source()

    assert SoftDeleteTest.objects.get(id=view_id).first_name == "Pristine"


def test_delete_and_reset_to_source_both_permanently_remove_an_organic_row_with_no_source():
    organic = SoftDeleteTestNoSource.objects.create(label="temp")
    organic.delete()
    assert not SoftDeleteTestNoSource.objects.filter(pk=organic.pk).exists()

    other = SoftDeleteTestNoSource.objects.create(label="temp2")
    SoftDeleteTestNoSource(pk=other.pk).reset_to_source()
    assert not SoftDeleteTestNoSource.objects.filter(pk=other.pk).exists()


def test_a_tombstone_whose_source_row_is_gone_is_not_a_valid_target(db_cursor):
    """The one case where the FK trigger's tombstone exclusion decides the
    answer, and therefore the test that has to cover it.

    The trigger accepts a target found in *either* the base table (excluding
    tombstones) or the source table. A tombstone normally implies a source row
    behind it -- that is the only reason to write one -- so the source branch
    matches and the exclusion changes nothing. It stops being true when the
    vendor drops the row from the source table: the mask outlives what it was
    masking, and now the base row is on disk, invisible through the view, with
    nothing behind it. `AND NOT _overlay_deleted` is what keeps the reference
    from being accepted, and deleting the clause is exactly the mutation
    tests/probe_unreachable_mutants.py applies.

    An organic row used to cover this by accident, back when deleting one left
    a tombstone. It cannot any more -- per-row soft delete hard-deletes it, so
    there is no base row for the exclusion to exclude.
    """
    source = SoftDeleteTestSource.objects.create(first_name="Withdrawn")
    target_pk = -source.id
    SoftDeleteTest.objects.get(pk=target_pk).delete()
    source.delete()

    db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    db_cursor.execute("SET CONSTRAINTS ALL DEFERRED")

    with pytest.raises(IntegrityError, match="not found in any target table"):
        with transaction.atomic():
            SoftDeleteTestNote.objects.create(target_id=target_pk, text="new note")
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_a_deleted_organic_row_is_not_a_valid_target_for_a_new_reference(db_cursor):
    """Same refusal, different reason: an organic row is hard-deleted, so the
    target is simply gone rather than masked."""
    target = SoftDeleteTest.objects.create(first_name="Target")
    # Model.delete() nulls out instance.pk, and target_id=None would raise a
    # not-null IntegrityError that looks exactly like the one we're after.
    target_pk = target.pk
    target.delete()
    # Flush the delete-side guard now, while there are still no references, so
    # that what the assertion below catches can only be the insert-side check.
    # Left pending it fires first and reports "still references", which is also
    # true but is a different guard.
    db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    db_cursor.execute("SET CONSTRAINTS ALL DEFERRED")

    with pytest.raises(IntegrityError, match="not found in any target table"):
        with transaction.atomic():
            SoftDeleteTestNote.objects.create(target_id=target_pk, text="new note")
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_a_non_deleted_row_is_still_a_valid_target(db_cursor):
    target = SoftDeleteTest.objects.create(first_name="Target")

    with transaction.atomic():
        SoftDeleteTestNote.objects.create(target=target, text="fine")
        db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


# ------------------------------------------- what is actually left on the disk
#
# Every test above asks the view what it can see, and the view answers the same
# either way: a deleted row is gone from it. None of them asked what the base
# table still holds, which is why the flag could be set on rows that had nothing
# to mask without a single test objecting.
#
# The flag has exactly one job: keep a source row hidden, so that removing the
# base copy does not un-mask the vendor's original. A row with no source row
# behind it has nothing to mask, so flagging it leaves a row that is invisible
# to the view forever while still holding its index entries and its primary key.
# The decision is per row, not per model.


def base_rows(model):
    """(pk, tombstoned) for everything physically in the base table."""
    base = model.base_table()
    return sorted(base.objects.values_list("pk", "_overlay_deleted"))


def test_deleting_an_organic_row_removes_it_rather_than_flagging_it():
    """It shadows nothing, so a tombstone would be pure residue."""
    organic = SoftDeleteTest.objects.create(first_name="ours")
    pk = organic.pk
    assert base_rows(SoftDeleteTest) == [(pk, False)]

    organic.delete()

    assert base_rows(SoftDeleteTest) == [], "an organic row must not leave a tombstone"


def test_deleting_a_materialized_source_row_leaves_a_tombstone():
    """Here the flag is load-bearing: drop the base row and the vendor's
    original reappears through the view's UNION ALL."""
    source = SoftDeleteTestSource.objects.create(first_name="Original")
    view_id = -source.id
    SoftDeleteTest.objects.filter(id=view_id).update(first_name="Edited")

    SoftDeleteTest.objects.get(id=view_id).delete()

    assert base_rows(SoftDeleteTest) == [(view_id, True)]
    assert not SoftDeleteTest.objects.filter(id=view_id).exists(), "and it stays hidden"


def test_deleting_an_untouched_source_row_creates_the_tombstone():
    """Nothing was in the base table at all, so the tombstone has to be written
    for the anti-join to have anything to exclude on."""
    source = SoftDeleteTestSource.objects.create(first_name="Untouched")
    view_id = -source.id
    assert base_rows(SoftDeleteTest) == []

    SoftDeleteTest.objects.get(id=view_id).delete()

    assert base_rows(SoftDeleteTest) == [(view_id, True)]
    assert not SoftDeleteTest.objects.filter(id=view_id).exists()


def test_a_sourceless_model_never_flags_anything():
    """No source table, so no row here can ever need masking."""
    organic = SoftDeleteTestNoSource.objects.create(label="temp")
    pk = organic.pk

    organic.delete()

    assert base_rows(SoftDeleteTestNoSource) == []
    assert not SoftDeleteTestNoSource.objects.filter(pk=pk).exists()


def test_the_two_kinds_of_delete_coexist_on_one_model():
    """The point of deciding per row: the same model, the same statement, two
    outcomes depending on whether the row shadows anything."""
    source = SoftDeleteTestSource.objects.create(first_name="vendor")
    vendor_id = -source.id
    organic = SoftDeleteTest.objects.create(first_name="ours")

    SoftDeleteTest.objects.all().delete()

    assert base_rows(SoftDeleteTest) == [(vendor_id, True)], (
        "the vendor-backed row keeps its tombstone, the organic one leaves nothing"
    )
    assert not SoftDeleteTest.objects.exists()
    assert organic.pk not in [pk for pk, _ in base_rows(SoftDeleteTest)]
