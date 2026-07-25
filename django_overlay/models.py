import copy

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

from .strategies import Strategy, default_id_field


class OverlayConfigurationError(Exception):
    pass


def _default_strategy() -> Strategy:
    """Lets a project set its own default strategy via
    settings.DJANGO_OVERLAY_DEFAULT_STRATEGY instead of Strategy.UUID4. A
    model can still override this with .with_strategy(...)."""
    configured = getattr(settings, "DJANGO_OVERLAY_DEFAULT_STRATEGY", Strategy.UUID4)
    if not isinstance(configured, Strategy):
        raise ImproperlyConfigured(
            "settings.DJANGO_OVERLAY_DEFAULT_STRATEGY must be a django_overlay.strategies.Strategy "
            f"member (e.g. Strategy.NEGATIVE_ID), got {configured!r}."
        )
    return configured


def _default_soft_delete() -> bool:
    """Lets a project opt every model into soft delete by default via
    settings.DJANGO_OVERLAY_DEFAULT_SOFT_DELETE. A model can still override
    this with its own OverlayMeta.soft_delete."""
    configured = getattr(settings, "DJANGO_OVERLAY_DEFAULT_SOFT_DELETE", False)
    if not isinstance(configured, bool):
        raise ImproperlyConfigured(f"settings.DJANGO_OVERLAY_DEFAULT_SOFT_DELETE must be a bool, got {configured!r}.")
    return configured


# Options that emit DDL go on the base (managed=True) model; everything
# else (ordering, verbose_name, ...) goes on the view model, since that's
# what's actually queried.
_BASE_ONLY_META_OPTIONS = ("constraints", "indexes", "unique_together", "index_together", "db_table_comment")

# Neither model is a sound home for these: the view model is unmanaged, so
# create_permissions() silently skips it; the base model isn't something
# app code should reference. Reject instead of picking a bad default.
_UNSUPPORTED_META_OPTIONS = ("permissions", "default_permissions")

# Both models must agree on which app they belong to — Django can usually
# infer this from the module either model is defined in, but an explicit
# override needs to reach both, not just whichever side it happened to land on.
_BOTH_META_OPTIONS = ("app_label",)

# The metaclass sets these itself on both models — declaring your own would
# just get silently overwritten, so reject instead.
_FORCED_META_OPTIONS = {
    "db_table": "table naming is controlled entirely by OverlayMeta.table_name (defaults to the lowercased class name)",
    "managed": "the base model is always managed=True and the view model is always managed=False",
}


def _split_meta_options(model_name: str, user_meta) -> tuple[dict, dict]:
    if user_meta is None:
        return {}, {}
    options = {k: v for k, v in vars(user_meta).items() if not k.startswith("_")}
    forced = [k for k in _FORCED_META_OPTIONS if k in options]
    if forced:
        raise OverlayConfigurationError(
            f"{model_name}.Meta.{forced[0]} isn't supported on an OverlayModel — "
            f"{_FORCED_META_OPTIONS[forced[0]]}; it would just be silently overwritten."
        )
    unsupported = [k for k in _UNSUPPORTED_META_OPTIONS if k in options]
    if unsupported:
        raise OverlayConfigurationError(
            f"{model_name}.Meta.{unsupported[0]} isn't supported on an OverlayModel — there's no "
            "model to attach it to that makes sense (see _UNSUPPORTED_META_OPTIONS)."
        )
    base_options = {k: v for k, v in options.items() if k in _BASE_ONLY_META_OPTIONS + _BOTH_META_OPTIONS}
    view_options = {k: v for k, v in options.items() if k not in _BASE_ONLY_META_OPTIONS}
    return base_options, view_options


class OverlayMeta:
    """Base class for a model's inner OverlayMeta. Subclass it (directly,
    or via with_strategy()) and add table_name / get_source()."""

    Strategy = Strategy
    strategy = _default_strategy()
    soft_delete = _default_soft_delete()
    pk_default_sql = None

    @classmethod
    def with_strategy(cls, strategy: Strategy):
        return type(f"OverlayMeta_{strategy.value}", (cls,), {"strategy": strategy})

    @staticmethod
    def get_source():
        raise NotImplementedError("OverlayMeta subclasses must implement get_source().")


class OverlayModelBase(models.base.ModelBase):
    """Splits one `class Person(OverlayModel)` into a hidden managed=True
    base table and the managed=False view model the app actually imports."""

    def __new__(mcs, name, bases, namespace, **kwargs):
        if namespace.pop("_overlay_root", False):
            return super().__new__(mcs, name, bases, namespace, **kwargs)

        is_overlay_subclass = any(isinstance(b, OverlayModelBase) for b in bases)
        meta = namespace.get("Meta")
        if not is_overlay_subclass or (meta is not None and getattr(meta, "abstract", False)):
            return super().__new__(mcs, name, bases, namespace, **kwargs)

        overlay_meta = namespace.pop("OverlayMeta", None)
        if overlay_meta is None or not issubclass(overlay_meta, OverlayMeta):
            raise OverlayConfigurationError(f"{name}.OverlayMeta must subclass django_overlay.models.OverlayMeta.")
        if "get_source" not in overlay_meta.__dict__:
            raise OverlayConfigurationError(
                f"{name}.OverlayMeta must implement get_source() returning a SourceTable | None."
            )
        if not isinstance(overlay_meta.strategy, Strategy):
            raise OverlayConfigurationError(
                f"{name}.OverlayMeta.strategy must be a Strategy member (e.g. Strategy.NEGATIVE_ID "
                f"or .with_strategy(...)), got {overlay_meta.strategy!r}."
            )
        if not isinstance(overlay_meta.soft_delete, bool):
            raise OverlayConfigurationError(
                f"{name}.OverlayMeta.soft_delete must be a bool, got {overlay_meta.soft_delete!r}."
            )

        # M2M fields go on the view model only — copying one to both models
        # would create two independent through tables.
        m2m_items = {k: v for k, v in namespace.items() if isinstance(v, models.ManyToManyField)}
        field_items = {k: v for k, v in namespace.items() if isinstance(v, models.Field) and k not in m2m_items}
        rest_items = {k: v for k, v in namespace.items() if k not in field_items and k not in m2m_items}
        table_name = getattr(overlay_meta, "table_name", name.lower())

        if overlay_meta.soft_delete and "_overlay_deleted" in field_items:
            raise OverlayConfigurationError(
                f"{name} can't declare its own `_overlay_deleted` field — django_overlay reserves "
                "that name for its soft_delete shadow flag."
            )

        if "id" not in field_items:
            injected = default_id_field(overlay_meta.strategy)
            if injected is not None:
                field_items["id"] = injected

        base_meta_options, view_meta_options = _split_meta_options(name, namespace.get("Meta"))

        base_ns = {**rest_items, **{k: copy.deepcopy(v) for k, v in field_items.items()}}
        if overlay_meta.soft_delete:
            # Base-only shadow flag — never copied to the view model, so it
            # never shows up as a queryable column there.
            base_ns["_overlay_deleted"] = models.BooleanField(default=False, editable=False)
        base_ns["__qualname__"] = f"{name}Base"
        # No default_permissions for the base table — nobody should see
        # "Can add <name>base" in an admin permission list.
        base_ns["Meta"] = type("Meta", (), {**base_meta_options, "db_table": table_name, "default_permissions": ()})
        base_model = super().__new__(mcs, f"{name}Base", bases, base_ns, **kwargs)

        view_ns = {**rest_items, **{k: copy.deepcopy(v) for k, v in field_items.items()}, **m2m_items}
        view_ns["Meta"] = type("Meta", (), {**view_meta_options, "db_table": f"{table_name}_view", "managed": False})
        view_model = super().__new__(mcs, name, bases, view_ns, **kwargs)

        view_model._base_model = base_model
        view_model._overlay_meta = overlay_meta
        view_model._is_overlay_view_model = True
        base_model._view_model = view_model
        return view_model


class OverlayModel(models.Model, metaclass=OverlayModelBase):
    _overlay_root = True

    Strategy = Strategy

    class Meta:
        abstract = True

    @classmethod
    def base_table(cls):
        """The hidden concrete model backing this view. Migration/tooling
        use only — application code should never query or write to it."""
        return cls._base_model

    @classmethod
    def get_source(cls):
        return cls._overlay_meta.get_source()

    def reset_to_source(self):
        """Discard this row's local materialization/soft-deletion and fall
        back to whatever the source shows for its id (nothing, if there's no
        source row). Not a delete — doesn't run Django's on_delete collector,
        since the identity itself isn't necessarily going away. See
        docs/concepts/DELETION.md."""
        self._base_model.objects.filter(pk=self.pk).delete()
