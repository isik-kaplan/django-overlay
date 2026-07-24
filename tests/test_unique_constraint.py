import pytest
from django.db import IntegrityError, transaction

from tests.testapp.models import UniqueTest, UniqueTestComposite
from tests.testapp_shared.models import UniqueTestCompositeSource, UniqueTestSource


pytestmark = pytest.mark.django_db


def test_organic_row_colliding_with_an_unmaterialized_source_row_is_rejected(db_cursor):
    UniqueTestSource.objects.create(ssn="123-45-6789")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            UniqueTest.objects.create(ssn="123-45-6789")
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_materializing_a_row_never_conflicts_with_its_own_source_origin(db_cursor):
    source = UniqueTestSource.objects.create(ssn="000-00-0000")
    view_id = -source.id

    with transaction.atomic():
        UniqueTest.objects.filter(id=view_id).update(ssn="000-00-0000")
        db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    assert UniqueTest.objects.get(id=view_id).ssn == "000-00-0000"


def test_materializing_a_row_that_collides_with_a_different_source_row_is_rejected(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    other_source = UniqueTestSource.objects.create(ssn="222-22-2222")
    view_id = -other_source.id

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            UniqueTest.objects.filter(id=view_id).update(ssn="111-11-1111")
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_two_distinct_ssns_are_both_allowed():
    UniqueTest.objects.create(ssn="333-33-3333")
    UniqueTest.objects.create(ssn="444-44-4444")


def test_composite_constraint_rejects_a_full_match_against_source(db_cursor):
    UniqueTestCompositeSource.objects.create(first_name="Jane", last_name="Doe")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            UniqueTestComposite.objects.create(first_name="Jane", last_name="Doe")
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_composite_constraint_allows_a_partial_match_against_source():
    UniqueTestCompositeSource.objects.create(first_name="Jane", last_name="Doe")
    # Only one of the two fields matches an existing source row — not a
    # conflict, since the constraint is on the (first_name, last_name) pair.
    UniqueTestComposite.objects.create(first_name="Jane", last_name="Smith")
