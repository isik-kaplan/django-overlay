from django.apps import apps
from django.core import checks
from django.db import models

from .constraints import OverlayUniqueConstraint
from .fields import OverlayForeignKey
from .uniqueness import suggested_constraint


@checks.register(checks.Tags.models)
def check_no_plain_fk_to_overlay_models(app_configs, **kwargs):
    """Fails `manage.py check` for a plain ForeignKey/OneToOneField
    pointing at an OverlayModel, or a ManyToManyField pointing at one
    without an explicit through= model."""
    errors = []
    for model in apps.get_models(include_auto_created=True):
        for field in model._meta.get_fields():
            if not field.is_relation:
                continue
            related_model = field.related_model
            if related_model is None or not getattr(related_model, "_is_overlay_view_model", False):
                continue

            # M2M fields are never "concrete" (even the declaring side), so
            # check the type directly instead.
            if isinstance(field, models.ManyToManyField):
                if not field.remote_field.through._meta.auto_created:
                    continue
                errors.append(
                    checks.Error(
                        f"{model.__name__}.{field.name} is a plain ManyToManyField pointing at "
                        f"{related_model.__name__}, which is a django_overlay view model. Django's "
                        "auto-created through table would use a plain ForeignKey, which can't hold "
                        "a real FK constraint against a view.",
                        hint="Use django_overlay.fields.OverlayManyToManyField (through=... is "
                        "required) or write your own explicit through model with OverlayForeignKey "
                        "fields.",
                        obj=field,
                        id="django_overlay.E002",
                    )
                )
                continue

            # Every concrete, non-M2M relation field Django has is a
            # ForeignKey or OneToOneField (or a subclass), so this is
            # already effectively "is a plain FK/O2O".
            if not getattr(field, "concrete", False):
                continue
            if isinstance(field, OverlayForeignKey):
                continue
            errors.append(
                checks.Error(
                    f"{model.__name__}.{field.name} is a plain {type(field).__name__} pointing at "
                    f"{related_model.__name__}, which is a django_overlay view model. Postgres cannot "
                    f"hold a real FK constraint against a view.",
                    hint=f"Use django_overlay.fields.OverlayForeignKey instead of {type(field).__name__}.",
                    obj=field,
                    id="django_overlay.E001",
                )
            )
    return errors


@checks.register(checks.Tags.models)
def check_overlay_uniqueness(app_configs, **kwargs):
    """Fails for any uniqueness rule on an OverlayModel that isn't an
    OverlayUniqueConstraint.

    An overlay model is queried through a view spanning the base table and the
    source table, so uniqueness has to hold across both. Every other way Django
    lets you declare it compiles down to a single index or table constraint on
    the base table alone, which accepts a value that already exists in the
    source. See django_overlay/uniqueness.py."""
    errors = []
    for model in apps.get_models():
        if not getattr(model, "_is_overlay_view_model", False):
            continue
        problems = unsupported_uniqueness(model)
        if problems:
            errors.append(uniqueness_error(model, problems))
    return errors


def unsupported_uniqueness(model):
    """[(complaint, fields, name), ...] for every uniqueness rule on `model`
    that isn't an OverlayUniqueConstraint.

    Read off the built models rather than the class namespace: `unique_together`
    and `constraints` land on the hidden base model, while the declared fields
    (including a OneToOneField, which the base model stores as the ForeignKey
    underneath) stay on the view model. Collected in one pass rather than
    reported one at a time — several successive boot failures to fix a single
    model is a miserable way to learn a rule."""
    base_meta = model._base_model._meta
    problems = []

    for entry in base_meta.unique_together:
        fields = (entry,) if isinstance(entry, str) else tuple(entry)
        problems.append((f"Meta.unique_together = {list(fields)}", fields, None))

    covered = set()
    for constraint in base_meta.constraints:
        if isinstance(constraint, OverlayUniqueConstraint):
            covered.add(tuple(constraint.fields))
            continue
        if not isinstance(constraint, models.UniqueConstraint):
            continue
        detail = f"Meta.constraints has a plain UniqueConstraint {constraint.name!r}"
        if constraint.condition is not None:
            detail += " (with a condition)"
        problems.append((detail, tuple(constraint.fields), constraint.name))

    for field in model._meta.fields:
        if field.primary_key or not field.unique:
            continue
        if isinstance(field, models.OneToOneField):
            # Keeps working — it just needs its uniqueness spelled out, since
            # the implicit one covers the base table only.
            if (field.name,) not in covered:
                problems.append(
                    (
                        f"{field.name} is a OneToOneField, whose implicit uniqueness covers your table only",
                        (field.name,),
                        None,
                    )
                )
        else:
            problems.append((f"{field.name} declares unique=True", (field.name,), None))
    return problems


def uniqueness_error(model, problems):
    table = model._base_model._meta.db_table
    complaints = "\n".join(f"  - {complaint}" for complaint, _, _ in problems)
    suggestions = "\n".join(f"        {suggested_constraint(table, fields, name)}," for _, fields, name in problems)
    hint = (
        "Declare them as OverlayUniqueConstraint in Meta.constraints instead:\n\n"
        f"    constraints = [\n{suggestions}\n    ]\n\n"
        "Those names are the ones django_overlay would have generated; any name that's "
        "unique across your models will do."
    )
    if any("with a condition" in complaint for complaint, _, _ in problems):
        hint += (
            "\n\nConditional uniqueness isn't supported at all: the source-side trigger has no "
            "way to apply the condition, so it would check for collisions the condition should "
            "have excluded. If you genuinely want a condition over your own rows only, add the "
            "partial index by hand in a RunSQL migration and leave it out of Meta."
        )
    return checks.Error(
        f"{model.__name__} declares uniqueness django_overlay can't honour:\n\n{complaints}\n\n"
        "An overlay model is queried through a view spanning your table and the source table, "
        "so uniqueness has to hold across both. Every one of the above compiles down to a "
        "single index on your table alone, which would accept a value that already exists in "
        "the source. OverlayUniqueConstraint adds the source-side check.",
        hint=hint,
        obj=model,
        id="django_overlay.E003",
    )
