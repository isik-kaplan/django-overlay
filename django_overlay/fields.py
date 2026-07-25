from django.db import models

from .models import OverlayConfigurationError
from .strategies import negates_source_ids


class OverlayForeignKey(models.ForeignKey):
    """FK for pointing at an OverlayModel. Postgres can't hold a real FK
    against a view, so this never creates a db constraint; referential
    integrity instead comes from a constraint trigger (see
    operations.AddOverlayConstraint)."""

    def __init__(self, to, *args, **kwargs):
        if "db_constraint" in kwargs:
            raise OverlayConfigurationError(
                "OverlayForeignKey always sets db_constraint=False (Postgres can't hold a real FK "
                "against a view) — don't pass db_constraint yourself."
            )
        kwargs["db_constraint"] = False
        super().__init__(to, *args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs.pop("db_constraint", None)
        return name, path, args, kwargs

    def trigger_name(self, model) -> str:
        return f"overlayfk_{model._meta.db_table}_{self.column}"[:63]

    def target_tables(self, tenant_schema: str) -> list[dict]:
        """The target's base table plus its source, if any — see target_tables_for()."""
        return target_tables_for(self.remote_field.model, tenant_schema)


def target_tables_for(target, tenant_schema: str) -> list[dict]:
    """[{"schema", "table", "id_column", "negate", "soft_delete"}, ...] for
    `target`'s base table (never negated) plus its source, if any (negated
    for a NEGATIVE_ID target). Takes the target model directly, not `self`, so a
    migration operation can call it against a *live* model even when the
    referencing field only exists in migration-historical state."""
    tables = [
        {
            "schema": tenant_schema,
            "table": target._base_model._meta.db_table,
            "id_column": "id",
            "negate": False,
            "soft_delete": target._overlay_meta.soft_delete,
        }
    ]
    source = target.get_source()
    if source is not None:
        negate = negates_source_ids(target._overlay_meta.strategy)
        tables.append(
            {
                "schema": source.schema,
                "table": source.table,
                "id_column": source.id_column,
                "negate": negate,
                "soft_delete": False,
            }
        )
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
