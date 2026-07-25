import pytest
from django.db.models import Q

from django_overlay.constraints import OverlayUniqueConstraint
from django_overlay.models import OverlayConfigurationError


def test_overlay_unique_constraint_rejects_a_condition():
    with pytest.raises(OverlayConfigurationError, match="condition"):
        OverlayUniqueConstraint(fields=["ssn"], name="fake", condition=Q(active=True))


def test_overlay_unique_constraint_without_a_condition_works_normally():
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="fake")
    assert constraint.fields == ("ssn",)
