import copy

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

    def referenced_row_trigger_name(self, model) -> str:
        """The delete-side guard. Lives on the target's base table, so the name
        has to identify the *referencing* side to stay unique among the
        triggers of a target with several things pointing at it."""
        return f"overlayfkdel_{model._meta.db_table}_{self.column}"[:63]

    def target_tables(self, tenant_schema: str) -> list[dict]:
        """The target's base table plus its source, if any — see target_tables_for()."""
        return target_tables_for(self.remote_field.model, tenant_schema)


def target_tables_for(target, tenant_schema: str, soft_delete: bool | None = None) -> list[dict]:
    """[{"schema", "table", "id_column", "negate", "soft_delete"}, ...] for
    `target`'s base table (never negated) plus its source, if any (negated
    for a NEGATIVE_ID target). Takes the target model directly, not `self`, so a
    migration operation can call it against a *live* model even when the
    referencing field only exists in migration-historical state.

    `soft_delete` can be overridden for the same reason the column list is
    taken from historical state: a trigger rebuilt while replaying an older
    migration must not reference `_overlay_deleted` before the migration that
    adds it has run."""
    tables = [
        {
            "schema": tenant_schema,
            "table": target._base_model._meta.db_table,
            "id_column": "id",
            "negate": False,
            "soft_delete": target._overlay_meta.soft_delete if soft_delete is None else soft_delete,
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


# A OneToOneField *is* a ForeignKey plus `unique=True` — that's the whole
# difference, in Django's source as well as conceptually. These pairs say which
# ForeignKey each OneToOneField collapses to. tests/test_field_internals.py
# pins the "plus unique=True and nothing else" assumption against Django.
_WITHOUT_IMPLICIT_UNIQUE = {
    models.OneToOneField: (models.ForeignKey, models.ManyToOneRel),
    OverlayOneToOneField: (OverlayForeignKey, models.ManyToOneRel),
}


def base_model_copy(field):
    """The base model's copy of a declared field.

    Almost always just a deep copy. The exception is a OneToOneField, which
    Django implements as "a ForeignKey that is also unique" — and it emits that
    uniqueness as a *table* constraint, which is the one shape django_overlay
    can't use: a table constraint covers the base table only (never the source)
    and can't carry the soft_delete predicate.

    So the base model stores the ForeignKey half and nothing else. The
    uniqueness comes from the OverlayUniqueConstraint that uniqueness.check()
    insists on, which does cover the source and can be made partial. The *view*
    model — the one application code holds — keeps the real OneToOneField, so
    `desk.occupant` is still singular and every O2O descriptor still works.

    Nothing observes the swap: the base model's relation is hidden
    (related_name="+"), so it installs no descriptor and appears in no reverse
    accessor either way.
    """
    copied = copy.deepcopy(field)
    collapsed = _WITHOUT_IMPLICIT_UNIQUE.get(type(field))
    if collapsed is not None:
        field_class, rel_class = collapsed
        # Retyped in place rather than rebuilt from deconstruct(): this runs
        # while the model class is still being created, and
        # ForeignKey.deconstruct() consults the app registry for swappable
        # models, which isn't loaded yet. Retyping is exact because
        # OneToOneField.__init__ adds no instance state beyond `unique`, and
        # OneToOneRel.__init__ none beyond `multiple`. Both are pinned by
        # tests/test_field_internals.py, which fails loudly — naming this
        # function — if a future Django adds to either.
        copied.__class__ = field_class
        copied.remote_field.__class__ = rel_class
        copied.remote_field.multiple = True  # OneToOneRel.__init__ sets this False
        copied._unique = False
        # Field.unique is a cached_property and deepcopy carries the cached
        # True over from the declared field, so clearing _unique isn't enough:
        # the schema editor would still emit UNIQUE.
        copied.__dict__.pop("unique", None)
    return copied


def hide_reverse_side(field) -> None:
    """Stop `field` claiming a reverse accessor on its target.

    Both models django_overlay builds declare every concrete field, so a
    relation with an explicit related_name would be declared twice against the
    same target — a fields.E304/E305 clash at boot. Hiding the base model's
    side also keeps Django's delete collector out of the hidden table: left
    visible, a cascade from the far end would delete base rows directly and
    walk straight past the view's INSTEAD OF triggers.

    Two attributes, because Django keeps two. `remote_field.related_name` is
    what the live model resolves accessors from; `_related_name` is what
    RelatedField.deconstruct() serializes, and leaving it alone would put a
    historical base model in migration state that re-claims the view model's
    accessor. tests/test_field_internals.py pins both.
    """
    field.remote_field.related_name = "+"
    field._related_name = "+"


class OverlayManyToManyField(models.ManyToManyField):
    """M2M field for relating to an OverlayModel. Requires an explicit
    through= model with OverlayForeignKey fields — Django's auto-created
    through table always uses a plain ForeignKey, which can never be safe
    against a view."""

    def __init__(self, to, *args, through, **kwargs):
        super().__init__(to, *args, through=through, **kwargs)
