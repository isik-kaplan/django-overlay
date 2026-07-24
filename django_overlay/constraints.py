from django.db import models


class OverlayUniqueConstraint(models.UniqueConstraint):
    """Also guards against a value already present in the source table.
    Postgres can't enforce that across a view's UNION ALL, so that half is
    a trigger instead (see AddOverlayUniqueConstraint)."""
