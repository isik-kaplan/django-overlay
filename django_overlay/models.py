import copy

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

from .strategies import Strategy, default_id_field


class OverlayConfigurationError(Exception):
    pass


def _default_strategy() -> Strategy:
    """settings.DJANGO_OVERLAY_DEFAULT_STRATEGY lets a project set its own
    project-wide default instead of Strategy.UUID4, without writing
    `.with_strategy(...)` on every model — a model can still override it
    explicitly the same way regardless of this setting."""
    configured = getattr(settings, "DJANGO_OVERLAY_DEFAULT_STRATEGY", Strategy.UUID4)
    if not isinstance(configured, Strategy):
        raise ImproperlyConfigured(
            "settings.DJANGO_OVERLAY_DEFAULT_STRATEGY must be a django_overlay.strategies.Strategy "
            f"member (e.g. Strategy.NEGATIVE_ID), got {configured!r}."
        )
    return configured


# The only Meta options that actually emit DDL — these have to land on the
# base (managed=True) model, since that's the only one Django generates
# schema for. Everything else the user declares (ordering, verbose_name,
# permissions, ...) lands on the view model instead, since that's what's
# actually queried.
_BASE_ONLY_META_OPTIONS = ("constraints", "indexes", "unique_together", "index_together", "db_table_comment")


def _split_meta_options(user_meta) -> tuple[dict, dict]:
    if user_meta is None:
        return {}, {}
    options = {k: v for k, v in vars(user_meta).items() if not k.startswith("_")}
    base_options = {k: v for k, v in options.items() if k in _BASE_ONLY_META_OPTIONS}
    view_options = {k: v for k, v in options.items() if k not in _BASE_ONLY_META_OPTIONS}
    return base_options, view_options


class OverlayMeta:
    """Base class for a model's inner OverlayMeta. Subclass it (directly,
    or via with_strategy()) and add table_name / get_source()."""

    Strategy = Strategy
    strategy = _default_strategy()
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

        # M2M fields have no column on either table, so they must only ever
        # be contributed to one class — deepcopying into both like an
        # ordinary field would make each copy independently create its own
        # through model.
        m2m_items = {k: v for k, v in namespace.items() if isinstance(v, models.ManyToManyField)}
        field_items = {k: v for k, v in namespace.items() if isinstance(v, models.Field) and k not in m2m_items}
        rest_items = {k: v for k, v in namespace.items() if k not in field_items and k not in m2m_items}
        table_name = getattr(overlay_meta, "table_name", name.lower())

        if "id" not in field_items:
            injected = default_id_field(overlay_meta.strategy)
            if injected is not None:
                field_items["id"] = injected

        base_meta_options, view_meta_options = _split_meta_options(namespace.get("Meta"))

        base_ns = {**rest_items, **{k: copy.deepcopy(v) for k, v in field_items.items()}}
        base_ns["__qualname__"] = f"{name}Base"
        base_ns["Meta"] = type("Meta", (), {**base_meta_options, "db_table": table_name})
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
