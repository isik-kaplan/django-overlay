"""OverlayMeta, and how a model's own Meta is divided between the two models
one declaration produces.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from ..exceptions import OverlayConfigurationError
from ..strategies import Strategy


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
    """Soft delete is the default, because it is the one that makes `.delete()`
    behave the way Django users expect: the row stays gone.

    Without it, deleting a source-backed row only drops your local copy, so the
    row reappears showing the vendor's pristine values — correct for the
    architecture, surprising as a default. A model that genuinely wants that
    (or a purely organic model, where a tombstone masks nothing and costs an
    index entry forever) can set `soft_delete = False`, and a project can flip
    the default back with settings.DJANGO_OVERLAY_DEFAULT_SOFT_DELETE."""
    configured = getattr(settings, "DJANGO_OVERLAY_DEFAULT_SOFT_DELETE", True)
    if not isinstance(configured, bool):
        raise ImproperlyConfigured(f"settings.DJANGO_OVERLAY_DEFAULT_SOFT_DELETE must be a bool, got {configured!r}.")
    return configured


# Options that emit DDL go on the base (managed=True) model; everything
# else (ordering, verbose_name, ...) goes on the view model, since that's
# what's actually queried. `constraints` can't simply go on both — duplicate
# constraint names across two models is models.E032 — so the view model
# reaches them through OverlayModel.get_constraints() instead.
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
    or via with_strategy()) and add table_name / get_source().

    `overridable = False` says a source row can never be edited in place —
    the tenant may add their own rows and (with soft_delete) hide vendor ones,
    but copy-on-write is refused. The clearest case is a many-to-many `through`
    model: a link row is a pair of ids, so there is nothing in it to edit.

    Declaring that buys a much cheaper view. The `NOT EXISTS` anti-join exists
    to stop a materialised row appearing twice; with nothing ever materialised
    it either disappears (hard delete) or narrows to tombstones only (soft
    delete), and unfiltered ordering goes from an `Append` over a
    `Hash Anti Join` to a `Merge Append` that `LIMIT` can stop early.

    It is enforced rather than assumed: the INSTEAD OF UPDATE trigger raises
    instead of copying the row down, so raw SQL cannot break the invariant the
    view now depends on either.

    Changing it does not generate a migration — like get_source(), it is
    OverlayMeta rather than Django model state, so nothing in the field list
    changes for makemigrations to notice. Run `manage.py resync_overlay_views`
    afterwards.

    No get_source() stub here on purpose: the metaclass requires every concrete
    overlay model to define one in its own OverlayMeta, so a NotImplementedError
    fallback could never run — and it read as reachable while being invisible to
    coverage, which excludes `raise NotImplementedError`."""

    Strategy = Strategy
    strategy = _default_strategy()
    soft_delete = _default_soft_delete()
    overridable = True
    pk_default_sql = None

    @classmethod
    def with_strategy(cls, strategy: Strategy):
        return type(f"OverlayMeta_{strategy.value}", (cls,), {"strategy": strategy})
