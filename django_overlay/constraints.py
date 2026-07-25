from django.db import models

from .models import OverlayConfigurationError


class OverlayUniqueConstraint(models.UniqueConstraint):
    """Also guards against a value already present in the source table.
    Postgres can't enforce that across a view's UNION ALL, so that half is
    a trigger instead (see AddOverlayUniqueConstraint)."""

    def __init__(self, *args, **kwargs):
        if kwargs.get("condition") is not None:
            raise OverlayConfigurationError(
                "OverlayUniqueConstraint doesn't support condition= — the source-vs-base trigger "
                "has no way to apply it, so it would silently check for collisions the condition "
                "should have excluded."
            )
        super().__init__(*args, **kwargs)
