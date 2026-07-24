from django.db import models


class OverlayUniqueConstraint(models.UniqueConstraint):
    """A UniqueConstraint that also guards against a value that already
    exists in the model's single source table, not just other base-table
    rows. Postgres can't put a UNIQUE constraint across a view's UNION ALL,
    so that half is enforced by a trigger instead — see
    operations.AddOverlayUniqueConstraint."""
