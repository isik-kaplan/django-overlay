from django.apps import apps
from django.core import checks
from django.db import models

from .fields import OverlayForeignKey


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
