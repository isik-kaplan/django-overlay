"""How many overlay views a statement reads, and the nested-loop ban built on
that count.

The most self-contained part of this package: it talks to the model registry
and to one Postgres session setting, and to nothing else here. Everything in
it exists because of one measurement -- two overlay views joined together do
not finish at 1,000,000 rows, and the reason is not the estimate but which
plan the estimate selects. See `_hash_joins_forced`.
"""

import contextlib
import functools

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _force_hash_joins_enabled() -> bool:
    """settings.DJANGO_OVERLAY_FORCE_HASH_JOINS turns the nested-loop ban off.

    On by default. Two overlay views joined together do not finish at
    1,000,000 rows, and the reason is not the estimate — it is which plan the
    estimate selects. See `_hash_joins_forced()`."""
    configured = getattr(settings, "DJANGO_OVERLAY_FORCE_HASH_JOINS", True)
    if not isinstance(configured, bool):
        raise ImproperlyConfigured(f"settings.DJANGO_OVERLAY_FORCE_HASH_JOINS must be a bool, got {configured!r}.")
    return configured


# Two thresholds, because a LIMIT changes how little it takes to go wrong.
#
# With a LIMIT the tuple fraction collapses, `add_path()` selects on startup
# cost alone, and a nested loop looks free however wrong the estimate is. One
# join is enough: a single-hop ordered page measured 7,854ms unbanned against
# 581ms banned, reproducibly. Two views means one join, so that is the floor.
#
# Without a LIMIT the planner has to cost the whole result honestly, and a
# single blind estimate survives that. It takes two hops -- five views -- for
# the estimates to compound into one that selects an unbounded plan.
#
# The gap between them is not caution, it is a measured regression. Banning
# nested loops on a *selective* filter with no LIMIT made it 7ms -> 60ms: 200
# rows is exactly where a nested loop is the right plan, and forbidding it buys
# a hash build over a large relation for nothing. One hop, no LIMIT, must be
# left alone.
#
# Both numbers come from what is now benchmark/suites/ban.py -- two passes
# agreeing against a noise floor measured at 1.0x -- and from suites/hops.py for
# how they hold as a saved search gets deeper. (The measurements were taken in
# tests/probe_hash_join_ban.py, which those suites replaced; see
# docs/development/BENCHMARKS.md for the mapping.)
#
# An earlier single threshold of 2 shipped that 7ms -> 60ms regression; an
# earlier single threshold of 4 blocked the 14x ordered-page win. Neither number
# alone fits the data.
#
# Both are pinned from both sides at scale 1.0, two passes agreeing, and the
# unsliced one is now determined exactly rather than to within a range. Banning
# at 3 views with no LIMIT costs 18ms -> 57ms; banning at 4 costs 3ms -> 36ms;
# not banning at 5 is >30s against 752ms. So it must not fire at 4 and must fire
# at 5, and 5 is the only integer that does both.
#
# It was 4 until the 4-view shape was measured, on the reasoning that 4 and 5
# behave identically on every m2m shape -- one hop is 3 views and two are 5 --
# so 4 was the smallest integer above one hop and nothing distinguished it from
# 5. Something does. Views are counted distinct, so an m2m hop steps 3 -> 5 -> 7
# and never lands on 4; but starting from a *through* model and traversing out of
# it does: person_address, person, person_phone, phone. That is one hop with an
# extra base view, and at 45 rows the ban made it 3ms -> 36ms -- the same shape
# of regression that ruled out a single threshold of 2, one view further along.
#
# Which says what the count actually is: a proxy for how many hops the planner
# has to estimate blindly, and a leaky one. Four views is where it leaks, because
# a query can reach four with one hop. The broad version of the same shape is
# x1.1 either way, so nothing is given up by not banning it.
#
# The same run is why there are two numbers and not one, stated as sharply as it
# gets: at 3 views, unsliced wants the nested loop kept (banning costs 3.2x) and
# sliced wants it banned (8751ms -> 1137ms). Identical shape, opposite verdict,
# decided entirely by the LIMIT.
#
# And a warning about measuring this at a convenient scale, because it nearly
# went the other way. At scale 0.3 the ban *costs* 5% at two hops and 13% at
# three and only pays at four, which reads as a threshold set a hop too low and
# argues for raising it to 6. That reading is wrong, and the paragraph above
# about the forced hash growing with the relation is why: at 1,000,000 rows the
# same two-hop shape is >30s unbanned against 752ms banned, and at three hops
# >30s against 3851ms. Raising the threshold on the scale-0.3 evidence would
# have put a >30s query back into the exact case the mechanism exists for. The
# low-scale cost is real; it is the price of the shape that does not finish
# without it, and it is the right trade.
_HASH_JOIN_THRESHOLD = 5
_HASH_JOIN_THRESHOLD_LIMITED = 2


@functools.lru_cache(maxsize=1)
def _overlay_view_tables() -> frozenset:
    """Every overlay view's table name.

    Cached: the model registry does not change after apps load, and this is
    consulted once per compiled query.
    """
    return frozenset(
        model._meta.db_table
        for model in apps.get_models()
        if getattr(model, "_is_overlay_view_model", False)
    )


# A saved-search compile can nest a few levels; a runaway is a bug elsewhere.
_MAX_SUBQUERY_DEPTH = 10


def _nested_queries(query):
    """Every Query nested inside this one, one level down.

    Counting only `alias_map` misses the shape that matters most. A scope
    attached as `pk__in=<queryset>` leaves the outer query with a single view
    and puts the other five in a subquery -- but they are all in the one
    statement Postgres plans, and it is their total that decides whether it
    picks a nested loop. Reading the outer query alone saw 1, declined to act,
    and let a summary over a two-hop scope run past four minutes.
    """
    found = []
    nodes = list(query.where.children) if query.where is not None else []
    while nodes:
        node = nodes.pop()
        children = getattr(node, "children", None)
        if children is not None:
            nodes.extend(children)
            continue
        for side in (getattr(node, "rhs", None), getattr(node, "lhs", None)):
            # A bare Query on the rhs of `__in`, or one wrapped in Subquery /
            # Exists, which keep theirs on `.query`.
            inner = getattr(side, "query", side)
            if hasattr(inner, "alias_map"):
                found.append(inner)
    for annotation in getattr(query, "annotations", {}).values():
        # `annotation`, not None, as the fallback -- the same shape as the
        # rhs/lhs line above and for the same reason. Django does not keep a
        # Subquery annotation as a Subquery: resolving it stores the inner
        # Query itself, which has no `.query` of its own, so falling back to
        # None dropped exactly the annotation this walk exists to find.
        # `Exists` keeps its query on `.query` and still resolves through the
        # first branch; an aggregate like Count() has neither and is skipped by
        # the alias_map check, which is what should happen to it.
        inner = getattr(annotation, "query", annotation)
        if hasattr(inner, "alias_map"):
            found.append(inner)
    return found


def _overlay_views_read(query, _depth=0, _found=None) -> set:
    """The distinct overlay views this statement reads, subqueries included.

    Distinct, not a count of occurrences, because `_m2m_fence()` deliberately
    adds a subquery over the through and target views that the outer query is
    already joined to. Counting occurrences made a single m2m hop score seven
    and trip a threshold meant to separate one hop from two -- the fence's own
    redundancy read as query complexity. What the planner's difficulty actually
    tracks is how many different unestimable relations it has to size, and a
    table it has already seen is not another one.
    """
    found = set() if _found is None else _found
    if _depth > _MAX_SUBQUERY_DEPTH:
        return found
    views = _overlay_view_tables()
    for join in query.alias_map.values():
        table = getattr(join, "table_name", None)
        if table in views:
            found.add(table)
    for inner in _nested_queries(query):
        _overlay_views_read(inner, _depth + 1, found)
    return found


def _overlay_views_joined(query) -> int:
    return len(_overlay_views_read(query))


@contextlib.contextmanager
def _hash_joins_forced(connection):
    """Ban nested loops for the statement inside, then put the setting back.

    The estimate cannot be repaired. An appendrel parent carries no statistics,
    `examine_simple_variable()` has no arm for one, and nothing in Postgres
    supplies a joint selectivity for two appendrel predicates — measured at
    266,974,515,000 estimated rows against 132 actual.

    It does not need to be repaired. A wrong estimate is only fatal when it
    selects an unbounded plan: the cheapest-startup path becomes a nested loop
    betting on an early exit that never arrives. Forbid that one plan and the
    same wrong estimate is harmless — the same query, still estimating
    266,974,515,000 rows, finishes in 457ms as a merge join.

    This changes the plan and not the query, so the rows are identical. That is
    the same bar the m2m fence and the select_related redirect are held to, and
    the reason this can be on by default.

    `SET` and restore, rather than `SET LOCAL`: LOCAL reverts at the end of the
    *transaction*, so inside a caller's long atomic block it would leave nested
    loops banned for every later statement in that block. The previous value is
    read back rather than assumed, so a caller who has deliberately set it
    keeps their setting afterwards.
    """
    with connection.cursor() as cursor:
        cursor.execute("SHOW enable_nestloop")
        previous = cursor.fetchone()[0]
        cursor.execute("SET enable_nestloop = off")
    try:
        yield
    finally:
        # Whitelisted rather than interpolated as-is. It comes from Postgres so
        # it is already safe, but a GUC value reaching an unparameterised SET is
        # the kind of thing that stops being safe when someone reuses it.
        restored = "on" if previous == "on" else "off"
        with connection.cursor() as cursor:
            cursor.execute(f"SET enable_nestloop = {restored}")


def _ban_nested_loops(compiler):
    """Wrap this compiler's execute_sql so its statement runs with the ban.

    Patched onto the instance rather than done by subclassing, because the
    compiler class comes from the database backend and replacing it would mean
    tracking whichever one the backend chose. The instance is created per
    execution and discarded, so nothing outlives the query.

    Wrapping `execute_sql` rather than the cursor is deliberate: it is the one
    choke point every read goes through — iteration, `count()`, `aggregate()`,
    `exists()` — and by the time it returns, the statement has been executed
    and its plan chosen, so restoring the setting afterwards cannot affect it.
    """
    execute_sql = compiler.execute_sql

    @functools.wraps(execute_sql)
    def with_ban(*args, **kwargs):
        with _hash_joins_forced(compiler.connection):
            return execute_sql(*args, **kwargs)

    compiler.execute_sql = with_ban
    return compiler
