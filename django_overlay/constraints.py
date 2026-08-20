from django.db import models

from .exceptions import OverlayConfigurationError


# The predicate every uniqueness rule on a soft_delete model is narrowed by, so
# a tombstoned row stops reserving its value. See uniqueness.py.
SOFT_DELETE_CONDITION = models.Q(_overlay_deleted=False)


class OverlayUniqueConstraint(models.UniqueConstraint):
    """Also guards against a value already present in the source table.
    Postgres can't enforce that across a view's UNION ALL, so that half is
    a trigger instead (see AddOverlayUniqueConstraint)."""

    def __init__(self, *args, soft_delete: bool = False, **kwargs):
        if kwargs.get("condition") is not None:
            raise OverlayConfigurationError(
                "OverlayUniqueConstraint doesn't support condition= — the source-vs-base trigger "
                "has no way to apply it, so it would silently check for collisions the condition "
                "should have excluded."
            )
        # `soft_delete` is set by the metaclass, never by hand. It turns the
        # constraint into a partial unique index excluding tombstoned rows,
        # which is what lets a soft-deleted row's value be reused. Carried as
        # its own flag rather than a caller-supplied condition so it survives
        # the migration round-trip without tripping the check above.
        self.soft_delete = soft_delete
        if soft_delete:
            kwargs["condition"] = SOFT_DELETE_CONDITION
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        path, args, kwargs = super().deconstruct()
        if self.soft_delete:
            # The condition is ours, derived from soft_delete, so soft_delete
            # is what gets serialized — passing the condition back in would hit
            # the check above. Popped only in this branch: a condition present
            # without soft_delete would be a bug, and silently dropping it here
            # would hide it.
            del kwargs["condition"]
            kwargs["soft_delete"] = True
        return path, args, kwargs

    def without_soft_delete_narrowing(self):
        """A copy that ignores nothing — the form used for validation, where
        the queryset already runs against the view and the view already hides
        tombstoned rows."""
        if not self.soft_delete:
            return self
        path, args, kwargs = self.deconstruct()
        kwargs.pop("soft_delete")
        return type(self)(*args, **kwargs)
