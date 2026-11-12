from django.db.models import Q

from django_overlay.constraints import OverlayUniqueConstraint


def test_overlay_unique_constraint_accepts_a_condition():
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="fake", condition=Q(active=True))
    assert constraint.condition == Q(active=True)
    assert constraint.own_condition == Q(active=True)


def test_overlay_unique_constraint_without_a_condition_works_normally():
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="fake")
    assert constraint.fields == ("ssn",)
