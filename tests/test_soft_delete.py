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


def test_a_soft_deleted_row_is_not_a_valid_target_for_a_new_reference(db_cursor):
    target = SoftDeleteTest.objects.create(first_name="Target")
    target.delete()

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            SoftDeleteTestNote.objects.create(target_id=target.pk, text="new note")
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_a_non_deleted_row_is_still_a_valid_target(db_cursor):
    target = SoftDeleteTest.objects.create(first_name="Target")

    with transaction.atomic():
        SoftDeleteTestNote.objects.create(target=target, text="fine")
        db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
