"""full_clean()/ModelForm validation on the view model.

Meta.constraints and Meta.unique_together live on the hidden base model — see
django_overlay.models._BASE_ONLY_META_OPTIONS — so without OverlayModel's
get_constraints()/_get_unique_checks() overrides none of this fires and the
first sign of a duplicate is an IntegrityError at COMMIT.
"""

import pytest
from django import forms
from django.core.exceptions import ValidationError

from tests.testapp.models import MetaTest, UniqueTest, UniqueTestComposite
from tests.testapp_shared.models import UniqueTestCompositeSource, UniqueTestSource


pytestmark = pytest.mark.django_db


def test_full_clean_catches_a_duplicate_of_a_local_row():
    UniqueTest.objects.create(ssn="123")

    with pytest.raises(ValidationError) as exc_info:
        UniqueTest(ssn="123").full_clean()

    assert "ssn" in exc_info.value.message_dict


def test_full_clean_catches_a_duplicate_of_an_untouched_source_row():
    # The whole point of validating against the view rather than the base
    # table: this row exists only in the source.
    UniqueTestSource.objects.create(ssn="456")

    with pytest.raises(ValidationError) as exc_info:
        UniqueTest(ssn="456").full_clean()

    assert "ssn" in exc_info.value.message_dict


def test_full_clean_accepts_a_value_that_collides_with_nothing():
    UniqueTest(ssn="789").full_clean()


def test_full_clean_does_not_flag_a_row_against_itself():
    obj = UniqueTest.objects.create(ssn="111")

    obj.notes = "edited"
    obj.full_clean()


def test_validate_constraints_reports_the_base_models_constraints():
    UniqueTest.objects.create(ssn="222")

    with pytest.raises(ValidationError):
        UniqueTest(ssn="222").validate_constraints()


def test_composite_unique_constraint_is_validated_against_the_source():
    UniqueTestCompositeSource.objects.create(first_name="Ada", last_name="Lovelace")

    with pytest.raises(ValidationError):
        UniqueTestComposite(first_name="Ada", last_name="Lovelace").full_clean()

    UniqueTestComposite(first_name="Ada", last_name="Byron").full_clean()


def test_check_constraints_are_validated_too():
    with pytest.raises(ValidationError):
        MetaTest(name="").full_clean()


def test_a_multi_field_constraint_is_validated():
    MetaTest.objects.create(name="Grace")

    with pytest.raises(ValidationError):
        MetaTest(name="Grace").full_clean()


def test_a_constraint_check_is_skipped_when_a_field_is_excluded():
    MetaTest.objects.create(name="Ada")

    MetaTest(name="Ada").validate_constraints(exclude={"name"})


def test_a_model_form_surfaces_the_collision_as_a_field_error():
    UniqueTestSource.objects.create(ssn="333")

    class UniqueTestForm(forms.ModelForm):
        class Meta:
            model = UniqueTest
            fields = ["ssn", "notes"]

    form = UniqueTestForm(data={"ssn": "333", "notes": ""})

    assert not form.is_valid()
    assert "ssn" in form.errors


def test_a_model_form_accepts_a_free_value():
    class UniqueTestForm(forms.ModelForm):
        class Meta:
            model = UniqueTest
            fields = ["ssn", "notes"]

    form = UniqueTestForm(data={"ssn": "444", "notes": "x"})

    assert form.is_valid(), form.errors
    assert form.save().pk is not None


REJECTED_BULK_CREATE = (
    "bulk_create({kwarg}=True) isn't supported on UniqueTest — it's an overlay model, so "
    "the insert targets a view and Postgres has no unique index there to detect a conflict "
    "against. Catch IntegrityError around a plain bulk_create(), or filter the batch "
    "against the view first."
)


def test_bulk_create_rejects_ignore_conflicts():
    """Matching on "ignore_conflicts" passes for a message that says nothing
    else -- eight mutants lived in the sentences after it, which are the only
    place the caller is told what to do instead."""
    from django_overlay.models import OverlayConfigurationError

    with pytest.raises(OverlayConfigurationError) as raised:
        UniqueTest.objects.bulk_create([UniqueTest(ssn="x")], ignore_conflicts=True)

    assert str(raised.value) == REJECTED_BULK_CREATE.format(kwarg="ignore_conflicts")


def test_bulk_create_rejects_update_conflicts():
    from django_overlay.models import OverlayConfigurationError

    with pytest.raises(OverlayConfigurationError) as raised:
        UniqueTest.objects.bulk_create(
            [UniqueTest(ssn="y")], update_conflicts=True, update_fields=["notes"], unique_fields=["id"]
        )

    assert str(raised.value) == REJECTED_BULK_CREATE.format(kwarg="update_conflicts")


def test_a_plain_bulk_create_forwards_its_arguments():
    """The pass-through branch hands five arguments to super(), and each of
    them had a mutant replacing it with None. batch_size is the observable
    one: it decides how many INSERTs are issued."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    objs = [UniqueTest(ssn=f"batch{n}") for n in range(4)]
    with CaptureQueriesContext(connection) as captured:
        UniqueTest.objects.bulk_create(objs, batch_size=1)

    inserts = [q for q in captured.captured_queries if "INSERT INTO" in q["sql"].upper()]
    assert len(inserts) == 4, "batch_size=1 should produce one INSERT per row"


def test_a_typo_in_a_keyword_is_still_an_error():
    """**kwargs has to reach super(), or an unknown keyword is swallowed.

    Django ignores update_fields when no conflict flag is set, so that pair
    proves nothing about forwarding. A keyword Django has never heard of does:
    forwarded it is a TypeError, dropped it is silence, and silence is how a
    misspelled argument gets shipped.
    """
    with pytest.raises(TypeError):
        UniqueTest.objects.bulk_create([UniqueTest(ssn="z")], ignore_conflcts=True)


def test_plain_bulk_create_still_works_and_returns_pks():
    objs = UniqueTest.objects.bulk_create([UniqueTest(ssn="b1"), UniqueTest(ssn="b2")])

    assert all(obj.pk is not None for obj in objs)
    assert UniqueTest.objects.filter(ssn__startswith="b").count() == 2


def test_a_model_declaring_its_own_manager_keeps_it():
    from django_overlay.models import OverlayQuerySet
    from tests.testapp.models import CustomManagerTest, LabelManager

    assert isinstance(CustomManagerTest.objects, LabelManager)
    assert not isinstance(CustomManagerTest.objects.all(), OverlayQuerySet)
    assert CustomManagerTest.objects.labelled().count() == 0


def test_a_model_without_its_own_manager_gets_the_overlay_queryset():
    from django_overlay.models import OverlayQuerySet

    assert isinstance(UniqueTest.objects.all(), OverlayQuerySet)
