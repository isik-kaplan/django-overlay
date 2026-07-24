from django.db import models

from .strategies import negates_source_ids


class OverlayForeignKey(models.ForeignKey):
    """FK for pointing at an OverlayModel. Postgres can't hold a real FK
    against a view, so this never creates a db constraint; referential
    integrity instead comes from a constraint trigger (see
    operations.AddOverlayConstraint)."""

    def __init__(self, to, *args, **kwargs):
        kwargs["db_constraint"] = False
        super().__init__(to, *args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs.pop("db_constraint", None)
        return name, path, args, kwargs

    def trigger_name(self, model) -> str:
        return f"overlayfk_{model._meta.db_table}_{self.column}"[:63]

    def target_tables(self):
        """[(table, id column, negate), ...]: the target's base table
        (never negated) plus its configured source, if any (negated for a
        NEGATIVE_ID target, since the referencing row stores the negated,
        view-presented id)."""
        target = self.remote_field.model
        tables = [(target._base_model._meta.db_table, "id", False)]
        source = target.get_source()
        if source is not None:
            negate = negates_source_ids(target._overlay_meta.strategy)
            tables.append((source.qualified_name, source.id_column, negate))
        return tables


class OverlayOneToOneField(OverlayForeignKey, models.OneToOneField):
    pass


class OverlayManyToManyField(models.ManyToManyField):
    """M2M field for relating to an OverlayModel. Requires an explicit
    through= model with OverlayForeignKey fields — Django's auto-created
    through table always uses a plain ForeignKey, which can never be safe
    against a view."""

    def __init__(self, to, *args, through, **kwargs):
        super().__init__(to, *args, through=through, **kwargs)
