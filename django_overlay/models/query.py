"""OverlayQuery: the Query subclass that rewrites a cross-view traversal into
a semi-join and fences an m2m hop.

Above `planning` and below `queryset` in this package: it asks planning how
many views a statement reads and decides whether to ban nested loops for it.
"""

import contextvars

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import connections, models
from django.db.models import sql
from django.db.models.constants import LOOKUP_SEP
from django.db.models.sql.where import AND, WhereNode

from ..fields import OverlayFencedIn, OverlayForeignKey, OverlayManyToManyField
from . import planning
from .planning import (
    _ban_nested_loops,
    _force_hash_joins_enabled,
    _hash_joins_forced,
    _overlay_views_joined,
)


def _m2m_fence_enabled() -> bool:
    """settings.DJANGO_OVERLAY_M2M_FENCE turns the m2m fence off on its own.

    Split out of DJANGO_OVERLAY_ARRAY_SUBQUERY_IN, which used to gate two
    unrelated things: the `fk__in=<subquery>` rewrite, and this. One flag for
    both meant that turning it off to spare a broad foreign-key filter also
    unfenced every m2m traversal -- 306.6ms -> 0.4ms selective and 7,896.5ms ->
    105.9ms broad, given away to fix something else.

    It gates whether the fence is *added*, not how it compiles, because a fence
    compiled as a plain `IN` is the one combination with no argument for it: an
    extra semi-join costed with the same blind appendrel estimate the fence
    exists to route around, carrying all of the cost and none of the benefit.
    On or absent -- there is nothing in between worth having.
    """
    configured = getattr(settings, "DJANGO_OVERLAY_M2M_FENCE", True)
    if not isinstance(configured, bool):
        raise ImproperlyConfigured(f"settings.DJANGO_OVERLAY_M2M_FENCE must be a bool, got {configured!r}.")
    return configured


def _rewrite_traversals_enabled() -> bool:
    """settings.DJANGO_OVERLAY_REWRITE_TRAVERSALS turns OverlayQuery's rewrite
    off. On by default, because leaving it off means every application author
    has to remember a 987x cliff forever."""
    configured = getattr(settings, "DJANGO_OVERLAY_REWRITE_TRAVERSALS", True)
    if not isinstance(configured, bool):
        raise ImproperlyConfigured(f"settings.DJANGO_OVERLAY_REWRITE_TRAVERSALS must be a bool, got {configured!r}.")
    return configured


_fence_suppressed = contextvars.ContextVar("django_overlay_fence_suppressed", default=False)


class OverlayQuery(sql.Query):
    """Rewrites `filter(fk__field=…)` across two overlay models into
    `filter(fk_id__in=<subquery>)`.

    A `UNION ALL` view is an appendrel and an appendrel parent carries no
    statistics, so a join between two of them is estimated at 1/200 and `LIMIT`
    then turns that into a nested loop that never terminates early. Measured at
    900,000 view rows: the traversal is 6,199.5ms, the subquery form is 6.3ms.
    See TODO/06 and tests/probe_join_fixes.py.

    There is no lookup to register for this — `customer__city` is resolved by
    `names_to_path()` well before lookups or transforms are consulted — so the
    hook is `build_filter()`, which sees the whole `('customer__city', 'x')`
    expression before any join is made.

    A join and a semi-join are only interchangeable when the join cannot
    multiply rows and cannot be negated into different NULL semantics, so the
    rewrite is gated hard. Everything it refuses is listed in
    `_traversal_rewrite()`, and tests/test_traversal_rewrite.py asserts the
    rewritten and unrewritten forms return identical rows for each of them.
    """

    def _wants_hash_joins(self) -> bool:
        if not _force_hash_joins_enabled():
            return False
        limited = self.high_mark is not None or self.low_mark
        # Through the module, not `from planning import _HASH_JOIN_THRESHOLD`.
        # The two thresholds are the only things here anyone overrides from
        # outside -- benchmark/suites/ban.py lowers one to measure what the ban
        # costs on the shapes it protects -- and a from-import binds the value
        # into *this* namespace, so an override elsewhere would not be seen.
        # That is not hypothetical: splitting models.py into a package moved
        # the binding out from under that benchmark, whose override then did
        # nothing at all and reported the ban as free. Read through `planning`
        # and there is one place to set it from anywhere. The functions above
        # are imported by name because nobody replaces them.
        threshold = planning._HASH_JOIN_THRESHOLD_LIMITED if limited else planning._HASH_JOIN_THRESHOLD
        return _overlay_views_joined(self) >= threshold

    def get_aggregation(self, using, *args, **kwargs):
        """`count()` and `aggregate()` do not always reach `get_compiler()`.

        When the query carries a DISTINCT, a slice, or a GROUP BY, Django
        cannot fold the aggregate into it and wraps it in an outer
        `sql.AggregateQuery` instead — a plain Query, not this class — then
        compiles and executes *that*. So the ban was skipped for exactly the
        shape most likely to need it: `.values("pk").distinct().count()`, which
        is how you count the people a saved search matches.

        It ran past 30s here while `values_list("pk", flat=True)` over the same
        scope, which does reach `get_compiler()`, returned in 608ms.
        `aggregate()` without an outer distinct hid the gap by needing no
        wrapper and working fine.

        Nesting is safe if `get_compiler()` also fires: `_hash_joins_forced()`
        reads the previous value rather than assuming it, so the inner restore
        puts back `off` and only the outer one puts back `on`.
        """
        if not self._wants_hash_joins():
            return super().get_aggregation(using, *args, **kwargs)
        with _hash_joins_forced(connections[using]):
            return super().get_aggregation(using, *args, **kwargs)

    def get_compiler(self, *args, **kwargs):
        """Ban nested loops when this query reads from more than one m2m hop's
        worth of overlay views. See `_hash_joins_forced()` for why, and
        `_HASH_JOIN_THRESHOLD` for where the line falls.

        Done here rather than in `build_filter()` because the decision needs
        the finished join list: a filter added early can be the one that tips
        the query over, and rewrites earlier in the pipeline can remove joins
        again. By compile time the alias map is settled.
        """
        compiler = super().get_compiler(*args, **kwargs)
        return _ban_nested_loops(compiler) if self._wants_hash_joins() else compiler

    def build_filter(self, filter_expr, *args, **kwargs):
        rewritten = self._traversal_rewrite(filter_expr, **kwargs)
        if rewritten is not None:
            return super().build_filter(rewritten, *args, **kwargs)

        fence = self._m2m_fence(filter_expr, **kwargs)
        clause, needed_inner = super().build_filter(filter_expr, *args, **kwargs)
        if fence is None:
            return clause, needed_inner

        # AND, never replace. See `_m2m_fence()` — the join stays exactly as
        # Django built it, so row multiplicity is untouched.
        fenced, fenced_inner = super().build_filter(fence, *args, **kwargs)
        combined = WhereNode([clause, fenced], connector=AND)
        return combined, list(needed_inner) + list(fenced_inner)

    def build_lookup(self, lookups, lhs, rhs):
        """Resolve the fence's private lookup name without registering it anywhere.

        `_m2m_fence()` returns `("pk__overlay_fenced_in", …)`, and a primary key
        is an ordinary `UUIDField` or `AutoField` that this package does not
        own — so the only way to make Django resolve that name through the
        normal path was `models.Field.register_lookup()`, which installs it on
        every field of every model in the importing project.

        Django only needs the name to resolve *here*: `names_to_path()` runs
        with `fail_on_missing=False`, so an unrecognised trailing name is
        handed to this method as a lookup rather than rejected as a bad field.
        Constructing the lookup directly therefore keeps the fence working and
        leaves every field in the project untouched — including, deliberately,
        overlay models' own primary keys. See `OverlayFencedIn` for why this is
        not offered as a public way to scope a query.
        """
        if lookups == [OverlayFencedIn.lookup_name]:
            return OverlayFencedIn(lhs, rhs)
        return super().build_lookup(lookups, lhs, rhs)

    def split_exclude(self, *args, **kwargs):
        """`exclude()` across a multi-valued relation.

        Django builds a fresh inner query here and then calls `trim_start()` on
        it, which assumes a particular join layout and indexes into the alias
        map by position. The inner query is an OverlayQuery too, and its filter
        is no longer negated, so the fence would fire inside it, add a subquery
        condition, and send `trim_start()` off the end of the list with an
        IndexError.

        Suppressing it costs nothing. The outer filter is negated, which the
        fence already refuses — see `_m2m_fence()` — so this only makes the
        inner query agree with the outer one.
        """
        token = _fence_suppressed.set(True)
        try:
            return super().split_exclude(*args, **kwargs)
        finally:
            _fence_suppressed.reset(token)

    def _m2m_fence(self, filter_expr, **kwargs):
        """An extra `(path, value)` to AND onto an m2m traversal, or None.

        `filter(phones__number=…)` joins two views through a third, and every
        one of them is an appendrel the planner has no statistics for. The
        forward-FK rewrite cannot be reused here: it *replaces* the join with a
        semi-join, and a semi-join does not multiply rows. A person with three
        matching phones must appear three times, and measuring it showed
        exactly that — the replacement returned 6 rows where the join returned
        10.

        So this adds a condition instead of replacing one:

            pk = ANY (ARRAY(SELECT person FROM through WHERE phone IN (…)))

        which is *implied by the join Django already emitted*. If a row
        satisfies the join then some through row links it to a matching target,
        so its pk is necessarily in that set. A conjunct implied by an existing
        conjunct cannot change which rows match or how many times each appears
        — it only hands the planner an InitPlan where it previously had a blind
        estimate.

        Measured on the production-shaped graph at 300,000 people: 306.6ms ->
        0.4ms for a selective term, 7,896.5ms -> 105.9ms for a broad one, with
        the row counts identical in both cases.
        """
        if kwargs.get("branch_negated") or kwargs.get("current_negated"):
            # Under negation the conjunct stops being redundant: NOT (A AND B)
            # is not NOT A. The whole argument above depends on the fence being
            # ANDed with the thing that implies it.
            return None
        if _fence_suppressed.get():
            return None
        if not isinstance(filter_expr, (tuple, list)) or len(filter_expr) != 2:
            return None
        path, value = filter_expr
        if not isinstance(path, str) or "__" not in path:
            return None
        if hasattr(value, "resolve_expression"):
            return None
        if not _rewrite_traversals_enabled():
            return None
        if not _m2m_fence_enabled():
            return None

        head, _, rest = path.partition("__")
        try:
            field = self.model._meta.get_field(head)
        except FieldDoesNotExist:
            return None

        ends = self._m2m_ends(field)
        if ends is None:
            return None
        through, target, to_here, to_there = ends

        if not getattr(through, "_is_overlay_view_model", False):
            return None
        if not getattr(target, "_is_overlay_view_model", False):
            return None
        if not self._is_fenceable_tail(target, rest):
            return None

        # OverlayQuery again, so the inner `phone__number=` hop rewrites into
        # its own fenced subquery and the whole thing nests.
        links = models.QuerySet(model=through, query=OverlayQuery(through))
        links = links.filter(**{f"{to_there}__{rest}": value})
        return ("pk__overlay_fenced_in", links.values(to_here))

    @staticmethod
    def _m2m_ends(field):
        """(through, other model, fk back to this model, fk to the other one).

        Both directions, because the two matter for different reasons. Forward
        is `filter(phones__kind=…)`, written by application code. Reverse is
        `filter(people__id=…)` — which nobody writes, but it is what a related
        manager and `prefetch_related()` emit, so it is the shape a detail page
        actually runs.
        """
        if isinstance(field, OverlayManyToManyField):
            declaring = field
            through = field.remote_field.through
            target = field.remote_field.model
            forward = True
        elif isinstance(field, models.ManyToManyRel) and isinstance(field.field, OverlayManyToManyField):
            declaring = field.field
            through = field.through
            target = declaring.model
            forward = False
        else:
            return None

        try:
            to_declaring = declaring.m2m_field_name()
            to_related = declaring.m2m_reverse_field_name()
        except Exception:  # noqa: BLE001 - a non-standard through model
            return None

        if forward:
            return through, target, to_declaring, to_related
        return through, target, to_related, to_declaring

    @staticmethod
    def _is_fenceable_tail(target, rest) -> bool:
        """Is `rest` something the through model can filter on?

        A field path is, including one ending at the far model's primary key —
        unlike a forward FK, `people__id` still joins through the link table,
        so there is a join here worth fencing.

        `in` and `exact` are, because they are what `prefetch_related()` emits
        (`filter(people__in=[…])`) and they still need that join.

        Anything else — `isnull`, a transform, an unknown lookup — is not.
        """
        if rest in {"in", "exact"}:
            return True
        head = rest.split(LOOKUP_SEP)[0]
        if head in {"pk"}:
            return True
        try:
            target._meta.get_field(head)
        except FieldDoesNotExist:
            return False
        return True

    def _traversal_rewrite(self, filter_expr, **kwargs):
        """The rewritten `(path, value)`, or None to leave it alone."""
        if kwargs.get("branch_negated") or kwargs.get("current_negated"):
            # exclude() / ~Q(). `NOT (col = ANY(ARRAY(…)))` and Django's
            # anti-join construction differ on a NULL foreign key, and being
            # subtly wrong here is worse than being slow.
            return None
        if not isinstance(filter_expr, (tuple, list)) or len(filter_expr) != 2:
            return None
        path, value = filter_expr
        if not isinstance(path, str) or "__" not in path:
            return None
        if hasattr(value, "resolve_expression"):
            # F(), OuterRef(), Subquery(): the expression would be resolved
            # against the inner model instead of this one.
            return None
        if not _rewrite_traversals_enabled():
            return None

        head, _, rest = path.partition("__")
        try:
            field = self.model._meta.get_field(head)
        except FieldDoesNotExist:
            return None
        # Forward relations only, which `isinstance` already guarantees: a
        # reverse or m2m path resolves to a ManyToOneRel/ManyToManyField rather
        # than to the OverlayForeignKey itself. That matters because a reverse
        # or m2m traversal multiplies rows and a semi-join does not, so swapping
        # one for the other would silently deduplicate.
        if not isinstance(field, OverlayForeignKey) or not field.concrete:
            return None

        target = field.remote_field.model
        if not getattr(target, "_is_overlay_view_model", False):
            # view -> plain measured 1.2-1.3x; the plain side's statistics
            # rescue the estimate and there is nothing to fence.
            return None
        if rest in {"pk", target._meta.pk.name, target._meta.pk.attname}:
            # Django trims this to a local column and never joins at all.
            return None
        try:
            target._meta.get_field(rest.split("__")[0])
        except FieldDoesNotExist:
            # `customer__isnull`, `customer__in`, `customer__exact`: a lookup
            # on the relation itself, not a traversal through it.
            return None

        # Built without a manager on purpose: a custom default manager may
        # filter rows, which would apply to the subquery and not to the join.
        # OverlayQuery again, so a multi-hop path rewrites all the way down.
        inner = models.QuerySet(model=target, query=OverlayQuery(target)).filter(**{rest: value})
        return (f"{field.attname}__in", inner.values("pk"))
