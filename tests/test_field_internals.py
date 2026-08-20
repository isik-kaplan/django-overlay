"""Assumptions django_overlay makes about Django's own field internals.

Each of these is load-bearing somewhere and none of it is public API, so it's
pinned here. A Django upgrade that invalidates one should fail with a pointed
message rather than quietly producing wrong DDL.
"""

import copy

import pytest
from django.db import models

from django_overlay.fields import base_model_copy, hide_reverse_side
from tests.testapp.models import SoftDeletePlainUniqueTest, Vendor


def make_pair(**kwargs):
    return (
        models.OneToOneField(Vendor, on_delete=models.CASCADE, **kwargs),
        models.ForeignKey(Vendor, on_delete=models.CASCADE, **kwargs),
    )


def test_a_one_to_one_field_is_a_foreign_key_plus_unique_and_nothing_else():
    """base_model_copy() retypes a OneToOneField in place instead of rebuilding
    it, which is only exact if the two classes' __init__ produce identical
    instance state apart from `unique`."""
    one_to_one, foreign_key = make_pair(null=True, related_name="x", db_column="y")

    differences = {
        key
        for key in set(one_to_one.__dict__) | set(foreign_key.__dict__)
        if one_to_one.__dict__.get(key) != foreign_key.__dict__.get(key)
    }

    # creation_counter is a global sequence, not per-class state.
    differences -= {"creation_counter"}

    assert differences == {"_unique", "remote_field"}, (
        "OneToOneField and ForeignKey now differ by more than `unique` — "
        "fields.base_model_copy can no longer retype one into the other."
    )


def test_a_one_to_one_rel_is_a_many_to_one_rel_plus_nothing():
    one_to_one, foreign_key = make_pair(null=True)

    o2o_rel, fk_rel = one_to_one.remote_field.__dict__, foreign_key.remote_field.__dict__
    differences = {key for key in set(o2o_rel) | set(fk_rel) if o2o_rel.get(key) != fk_rel.get(key)}

    assert differences == {"field", "multiple"}, (
        "OneToOneRel and ManyToOneRel now differ in instance state beyond `multiple` — "
        "fields.base_model_copy fixes up `multiple` and nothing else."
    )


def test_field_unique_is_still_a_cached_property():
    """If it stops being cached, the __dict__.pop in base_model_copy is dead
    code; if it stays cached and we stop popping, the schema editor emits a
    UNIQUE constraint we specifically don't want."""
    field = models.OneToOneField(Vendor, on_delete=models.CASCADE)

    assert "unique" not in field.__dict__
    assert field.unique is True
    assert field.__dict__["unique"] is True, "Field.unique is no longer a cached_property"


def test_deconstruct_serializes_the_declared_related_name_not_the_live_one():
    """The reason hide_reverse_side() has to set two attributes."""
    field = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="declared")
    field.remote_field.related_name = "+"

    _, _, _, kwargs = field.deconstruct()

    assert kwargs["related_name"] == "declared", (
        "RelatedField.deconstruct() now reads remote_field.related_name — "
        "fields.hide_reverse_side no longer needs to set _related_name."
    )


def test_hide_reverse_side_makes_deconstruct_agree_with_the_live_field():
    field = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="declared")

    hide_reverse_side(field)
    _, _, _, kwargs = field.deconstruct()

    assert field.remote_field.related_name == "+"
    assert kwargs["related_name"] == "+"


def test_base_model_copy_collapses_a_one_to_one_field():
    declared = models.OneToOneField(Vendor, on_delete=models.CASCADE, null=True)

    copied = base_model_copy(declared)

    assert type(copied) is models.ForeignKey
    assert type(copied.remote_field) is models.ManyToOneRel
    assert copied.remote_field.multiple is True
    assert not copied.unique
    assert declared.unique, "the declared field must be left alone"


def test_the_collapsed_copy_clears_a_unique_that_was_already_cached():
    """Field.unique is a cached_property and deepcopy carries the cached value
    over. By the time the metaclass copies a field Django has usually read it,
    so clearing _unique without evicting the cache leaves the schema editor
    still emitting UNIQUE. A copy of a *fresh* field wouldn't show this."""
    declared = models.OneToOneField(Vendor, on_delete=models.CASCADE)
    assert declared.unique, "read it first, the way model setup does"
    assert declared.__dict__["unique"] is True

    copied = base_model_copy(declared)

    assert "unique" not in copied.__dict__ or copied.__dict__["unique"] is False
    assert not copied.unique


def test_the_collapsed_copy_shares_no_state_with_the_declared_field():
    """A shallow copy would share remote_field, so hiding the base model's
    reverse side would silently hide the view model's too."""
    declared = models.OneToOneField(Vendor, on_delete=models.CASCADE, related_name="occupant")

    copied = base_model_copy(declared)
    hide_reverse_side(copied)

    assert copied.remote_field is not declared.remote_field
    assert declared.remote_field.related_name == "occupant"
    assert declared._related_name == "occupant"


def test_base_model_copy_leaves_other_fields_alone():
    declared = models.CharField(max_length=10)

    copied = base_model_copy(declared)

    assert type(copied) is models.CharField
    assert copied is not declared


@pytest.mark.parametrize("attribute", ["null", "db_column", "db_index", "db_constraint", "remote_field"])
def test_the_collapsed_copy_keeps_everything_that_shapes_the_column(attribute):
    declared = models.OneToOneField(Vendor, on_delete=models.CASCADE, null=True, db_column="vendor_ref")

    copied = base_model_copy(declared)

    if attribute == "remote_field":
        assert copied.remote_field.model is declared.remote_field.model
        assert copied.remote_field.on_delete is declared.remote_field.on_delete
    else:
        assert getattr(copied, attribute) == getattr(declared, attribute)


def test_the_live_base_model_matches_what_migrations_recorded():
    """Belt and braces on the whole arrangement: whatever the metaclass built,
    deconstruct() has to describe it, or state and reality drift."""
    base_field = SoftDeletePlainUniqueTest._base_model._meta.get_field("vendor")

    _, path, _, kwargs = base_field.deconstruct()

    assert path == "django.db.models.ForeignKey"
    assert kwargs.get("unique") in (None, False)
    assert kwargs["related_name"] == "+"


def test_deepcopy_of_a_declared_field_is_not_shared_with_the_view_model():
    view_field = SoftDeletePlainUniqueTest._meta.get_field("vendor")
    base_field = SoftDeletePlainUniqueTest._base_model._meta.get_field("vendor")

    assert view_field is not base_field
    assert isinstance(view_field, models.OneToOneField)
    assert copy.deepcopy(view_field).remote_field is not view_field.remote_field
