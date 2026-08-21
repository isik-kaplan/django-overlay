"""The overlay model machinery, split by subject.

One 1,227-line module until it was split, and the split is not cosmetic: a
mutation shard matches files rather than line ranges, so the whole of this
package's query machinery sat in one CI job that took 5.5 hours against a
6-hour cap. The five modules below are the seams that were already there --
nothing points backwards, so the move was a move and not a rewrite:

    planning  <-  query  <-  queryset  <-  base
                                  meta  <-'

Everything is re-exported here, so `from django_overlay.models import ...`
means what it always did -- including the private names the test suite and the
rest of the package reach for. A caller should not have to know which of the
five a name came from.
"""

# ruff: noqa: F401 -- every import here is a re-export, which is the point.
from ..exceptions import OverlayConfigurationError
from .base import OverlayModel, OverlayModelBase, _base_field_copy
from .meta import (
    _BASE_ONLY_META_OPTIONS,
    _BOTH_META_OPTIONS,
    _FORCED_META_OPTIONS,
    _UNSUPPORTED_META_OPTIONS,
    OverlayMeta,
    _default_soft_delete,
    _default_strategy,
    _split_meta_options,
)
from .planning import (
    _HASH_JOIN_THRESHOLD,
    _HASH_JOIN_THRESHOLD_LIMITED,
    _MAX_SUBQUERY_DEPTH,
    _ban_nested_loops,
    _force_hash_joins_enabled,
    _hash_joins_forced,
    _nested_queries,
    _overlay_view_tables,
    _overlay_views_joined,
    _overlay_views_read,
)
from .query import (
    OverlayQuery,
    _fence_suppressed,
    _m2m_fence_enabled,
    _rewrite_traversals_enabled,
)
from .queryset import (
    OverlayManager,
    OverlayQuerySet,
    _django_internal_lock,
    _reads_own_columns,
    _redirect_select_related_enabled,
)


__all__ = ["OverlayConfigurationError", "OverlayMeta", "OverlayModel", "OverlayModelBase", "OverlayQuerySet"]
