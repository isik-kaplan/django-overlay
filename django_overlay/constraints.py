from django.db import models


# The predicate every uniqueness rule on a soft_delete model is narrowed by, so
# a tombstoned row stops reserving its value. See uniqueness.py.
SOFT_DELETE_CONDITION = models.Q(_overlay_deleted=False)


class OverlayUniqueConstraint(models.UniqueConstraint):
    """Also guards against a value already present in the source table.
    Postgres can't enforce that across a view's UNION ALL, so that half is
    a trigger instead (see AddOverlayUniqueConstraint).

    `condition=` narrows it like a plain UniqueConstraint's: the base half
    becomes a partial unique index, and the trigger applies the same condition
    to the new row and to the source row it compares against. It may name only
    the model's own fields other than the primary key, which the source table
    has under the same column names; `manage.py check` enforces that
    (django_overlay.E007)."""

    def __init__(self, *args, soft_delete: bool = False, **kwargs):
        # `soft_delete` is set by the metaclass, never by hand. It narrows the
        # constraint to rows that aren't tombstoned, which is what lets a
        # soft-deleted row's value be reused. Carried as its own flag rather
        # than folded into the caller's condition for good, so the migration
        # round-trip writes back the condition the caller wrote.
        self.soft_delete = soft_delete
        self.own_condition = kwargs.get("condition")
        if soft_delete:
            kwargs["condition"] = (
                SOFT_DELETE_CONDITION if self.own_condition is None else self.own_condition & SOFT_DELETE_CONDITION
            )
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        path, args, kwargs = super().deconstruct()
        if self.soft_delete:
            # The tombstone half is ours, derived from soft_delete, so
            # soft_delete is what gets serialized, beside the caller's own.
            if self.own_condition is None:
                del kwargs["condition"]
            else:
                kwargs["condition"] = self.own_condition
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
