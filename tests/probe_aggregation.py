"""A summary panel over a whole list: totals, buckets, and reach counts.

The shape is one statement over a filtered list of people -- a total, a dozen
or so bucket counts over person columns, and "how many distinct phones /
emails / addresses does this list reach". No LIMIT, no ordering, hundreds of
thousands of people in scope.

This is a different shape from every other probe here. The rest measure point
lookups and ordered pages, where the overlay's cost is the planner
mis-estimating a join under a collapsed tuple fraction (see
probe_limit_trap). There is no LIMIT here to collapse it and no narrow filter
to plan around: the query reads the whole scope and folds it. So the question
is whether the overlay's per-row cost -- the UNION ALL and the anti-join above
it -- is even visible once the work is dominated by aggregation.

The normalisation matters more than the overlay does, and that is the point of
measuring three ways of writing the same summary:

  A. one .aggregate() with everything in it.
     Each relation count adds a join, the joins multiply, and so every
     person-column bucket must become Count(distinct=True) to survive that.
     Postgres cannot hash several distinct aggregates in one pass -- it runs a
     separate sort per aggregate. Twenty aggregates is twenty sorts.

  B. the person-column buckets in one query with no joins at all, and each
     relation count as its own query. Four statements, no distinct anywhere
     except the three relation counts, which genuinely need it.

  C. B, but with the scope resolved once into an id set that the four
     statements reuse, instead of each one re-running the scope filter. This
     is the shape a "resolve the list, then summarise it" implementation has,
     and the one that matters when the scope is itself join-heavy.

    OVERLAY_BENCH_SCALE=0.3 POSTGRES_USER=postgres uv run pytest \\
        --reuse-db tests/probe_aggregation.py -s -q -o addopts="" --no-cov

    OVERLAY_BENCH_SCALE=1.0 ...       # 1,000,000 people, the real size
"""

import os
import time
from datetime import date

import pytest
from django.core.exceptions import FieldError
from django.db import OperationalError, connection
from django.db.models import Count, Q
from django.db.models.constants import LOOKUP_SEP

from tests.probe_bench_graph import load
from tests.testapp.models import BenchPerson, PlainPerson


pytestmark = pytest.mark.django_db(transaction=True)

# 30s rather than 60s: five shapes over four scopes on two models is a lot of
# ceiling to pay for twice each, and nothing that needs more than 30s for one
# summary panel is a candidate for anything.
TIMEOUT_MS = int(os.environ.get("OVERLAY_AGG_TIMEOUT_MS", 30_000))

# Scopes, smallest first. A saved search over a person column is the cheap
# case; the last one is join-heavy, which is where resolving the scope once
# (shape C) can pay for itself.
SCOPES = (
    ("2.5% of people (25 cities)", {"city__in": [f"city{n}" for n in range(25)]}),
    ("50% of people (score < 500)", {"score__lt": 500}),
    ("everyone", {}),
    ("a join-heavy saved search", {"addresses__country": "US", "phones__kind": "mobile"}),
)

# Sixteen buckets over person columns, plus a total and three relation counts:
# twenty aggregates, which is the size a real summary panel runs to.
SCORE_BUCKETS = {
    "score_000_199": Q(score__lt=200),
    "score_200_399": Q(score__gte=200, score__lt=400),
    "score_400_599": Q(score__gte=400, score__lt=600),
    "score_600_799": Q(score__gte=600, score__lt=800),
    "score_800_plus": Q(score__gte=800),
}
STATUS_BUCKETS = {
    f"status_{status}": Q(status=status)
    for status in ("active", "lapsed", "pending", "closed")
}
DECADE_BUCKETS = {
    f"born_{decade}s": Q(born_on__gte=date(decade, 1, 1), born_on__lt=date(decade + 10, 1, 1))
    for decade in (1950, 1960, 1970, 1980, 1990, 2000, 2010)
}
BUCKETS = {**SCORE_BUCKETS, **STATUS_BUCKETS, **DECADE_BUCKETS}

# "How many distinct phones does this list reach", one per relation. These are
# the only aggregates that genuinely need DISTINCT -- the join multiplies.
RELATIONS = {"phone_count": "phones", "email_count": "emails", "address_count": "addresses"}


def cap_the_session():
    """Bound every statement, not just the measured ones.

    An earlier probe capped only the queries inside its timer, and an
    unmeasured sizing query ran away instead -- for over an hour, holding
    ACCESS SHARE on the bench views so the next run's TRUNCATE queued behind
    it forever. Killing pytest does not cancel a running backend. The
    lock_timeout is the other half: a run that cannot get its locks should
    fail in a second rather than wait.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = {TIMEOUT_MS}")
        cursor.execute("SET lock_timeout = 5000")


def buckets_for(distinct):
    """The person-column aggregates.

    `distinct` is not a style choice. With a relation join in the same query
    every person appears once per (address, phone, email) combination, so a
    plain Count over-counts by the multiplication factor. Shape A has to pay
    for it; shapes B and C avoid the join instead, which is the cheaper way to
    get the same number.
    """
    aggregates = {"total": Count("id", distinct=distinct)}
    for name, condition in BUCKETS.items():
        aggregates[name] = Count("id", distinct=distinct, filter=condition)
    return aggregates


def shape_a(model, scope):
    """Everything in one .aggregate(): twenty aggregates over a multiplied join."""
    return model.objects.filter(**scope).aggregate(
        **buckets_for(distinct=True),
        **{alias: Count(path, distinct=True) for alias, path in RELATIONS.items()},
    )


def scope_joins(scope):
    """Does this scope traverse a relation, and so multiply rows?

    `city__in` does not; `addresses__country` does. Anything with a `__` before
    a lookup name is a path through a relation -- and every scope in SCOPES
    that uses one is a relation path, so testing for the separator is enough
    here. A general implementation would have to walk `_meta`.
    """
    return any(LOOKUP_SEP in key for key in scope)


def shape_b(model, scope):
    """Buckets in one query, then one query per relation.

    The buckets need DISTINCT exactly when the *scope* joins. Hardcoding
    `distinct=False` here was wrong: it is right for a scope like `city__in`,
    which touches only the person table, and silently wrong for one like
    `addresses__country`, whose own join multiplies every person by their
    matching addresses. Measured at 1,000,000 people, that reported 206,664
    people where there were 59,999, with 14 of the 20 aggregates inflated --
    on plain tables as much as on the overlay, since it is Django's join
    semantics rather than anything about the view.
    """
    distinct = scope_joins(scope)
    summary = model.objects.filter(**scope).aggregate(**buckets_for(distinct=distinct))
    for alias, path in RELATIONS.items():
        summary |= model.objects.filter(**scope).aggregate(**{alias: Count(path, distinct=True)})
    return summary


def _resolved(model, scope, lookup, distinct):
    """The four statements of shape B, scoped by a subquery instead of by repeating
    the filter.

    A subquery rather than a materialised list of ids: half a million keys
    round-tripped into Python and back out as query parameters is its own
    bottleneck, and it is not what an ORM-only implementation would write.

    `lookup` picks how the subquery is attached, which is the whole experiment:

      "in"                -- what anyone would write. Plain Django. Note that
                             DJANGO_OVERLAY_ARRAY_SUBQUERY_IN does *not* reach
                             here: OverlaySubqueryIn is registered on
                             OverlayForeignKey, and this is a primary key.
      "overlay_fenced_in" -- the same `= ANY (ARRAY(...))` rewrite the m2m fence
                             uses, reachable only because OverlayFencedIn is
                             registered on models.Field under a private name.

    `distinct` is the other axis. Without it the subquery returns one row per
    joined combination, so a join-heavy scope hands the outer query several
    times more keys than there are people. `IN` dedups anyway, but `ARRAY()`
    materialises every duplicate.
    """
    inner = model.objects.filter(**scope).values("pk")
    if distinct:
        inner = inner.distinct()
    scoped = {f"pk__{lookup}": inner}
    summary = model.objects.filter(**scoped).aggregate(**buckets_for(distinct=False))
    for alias, path in RELATIONS.items():
        summary |= model.objects.filter(**scoped).aggregate(**{alias: Count(path, distinct=True)})
    return summary


def shape_c(model, scope):
    """Scope resolved once, attached with a plain `pk__in`."""
    return _resolved(model, scope, lookup="in", distinct=False)


def shape_d(model, scope):
    """Shape C plus `.distinct()` on the subquery -- does deduping alone do it?"""
    return _resolved(model, scope, lookup="in", distinct=True)


def shape_e(model, scope):
    """Shape D plus the array rewrite -- does `= ANY (ARRAY(...))` add anything?

    Overlay-only: `overlay_fenced_in` is resolved by OverlayQuery.build_lookup()
    and registered on no field, so a plain model cannot reach it at all. There
    is no plain baseline to print for this row, which is itself the honest
    reading -- an ordinary Django app has no way to write this.
    """
    return _resolved(model, scope, lookup="overlay_fenced_in", distinct=True)


SHAPES = (("A  one aggregate()", shape_a), ("B  split per relation", shape_b),
          ("C  scope: pk__in", shape_c), ("D  scope: pk__in distinct", shape_d),
          ("E  scope: fenced array", shape_e))
OVERLAY_ONLY = {"E  scope: fenced array"}


def timed(build, rounds=2, ceiling=15_000):
    """(best milliseconds, result). (timeout, None) if it is worse than the cap.

    Two rounds rather than three: several of these read the whole table, so a
    third round buys very little and costs a lot of wall clock.
    """
    best, result = None, None
    for _ in range(rounds):
        started = time.perf_counter()
        try:
            result = build()
        except OperationalError:
            return float(TIMEOUT_MS), None
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
        if best > ceiling:
            break
    return best, result


def test_aggregation():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s")
    cap_the_session()
    print(f"  {BenchPerson.objects.count():,} people in the view")

    # Which scoping form actually compiles to the array rewrite. Printed rather
    # than assumed: `pk__in` reads like it should pick up
    # DJANGO_OVERLAY_ARRAY_SUBQUERY_IN and does not, because that lookup is
    # registered on OverlayForeignKey and a primary key is not one. The plain
    # model is included to show that `overlay_fenced_in` is unreachable there --
    # OverlayQuery.build_lookup() resolves it, and PlainPerson has no such query.
    print("\n" + "=" * 104)
    print("WHICH SCOPING FORM COMPILES TO `= ANY (ARRAY(...))`?")
    print("=" * 104)
    for model in (BenchPerson, PlainPerson):
        for lookup in ("in", "overlay_fenced_in"):
            inner = model.objects.filter(city="city42").values("pk")
            try:
                statement, _ = model.objects.filter(**{f"pk__{lookup}": inner}).query.sql_with_params()
            except FieldError as error:
                verdict = f"unreachable ({type(error).__name__})"
            else:
                verdict = "ARRAY initplan" if "= ANY (ARRAY" in statement else "plain IN semi-join"
            print(f"  {model.__name__:<14} pk__{lookup:<20} {verdict}")

    for scope_label, scope in SCOPES:
        print("\n" + "=" * 104)
        print(f"SCOPE: {scope_label}")
        print("=" * 104)
        print(f"  {'shape':<26} {'overlay':>11} {'plain':>10} {'ratio':>9}   totals")
        print("  " + "-" * 96)

        # Every shape must agree with every other, on both models, or the
        # numbers below are timings of three different questions.
        agreed = {}
        for shape_label, shape in SHAPES:
            overlay_ms, overlay_result = timed(lambda f=shape, s=scope: f(BenchPerson, s))
            if shape_label in OVERLAY_ONLY:
                plain_ms, plain_result, ratio = float("nan"), None, float("nan")
            else:
                plain_ms, plain_result = timed(lambda f=shape, s=scope: f(PlainPerson, s))
                ratio = overlay_ms / plain_ms if plain_ms else float("nan")

            # Each model is checked against its own earlier shapes, independently.
            # Pairing them would throw away a good result whenever the other side
            # timed out -- which is exactly when a shape is most likely to be
            # answering a different question, since the thing that makes it slow
            # (a join that multiplies) is also the thing that corrupts its counts.
            notes = []
            sides = [("overlay", overlay_result)]
            if shape_label in OVERLAY_ONLY:
                notes.append("overlay-only, no plain equivalent")
            else:
                sides.append(("plain", plain_result))
            for name, result in sides:
                if result is None:
                    notes.append(f"{name} hit the {TIMEOUT_MS / 1000:.0f}s cap")
                    continue
                previous = agreed.setdefault(name, result)
                if previous != result:
                    differing = [k for k in result if previous[k] != result[k]]
                    notes.append(
                        f"{name} DISAGREES: total {result['total']:,} "
                        f"vs {previous['total']:,} ({len(differing)} fields)"
                    )
            reference = overlay_result or plain_result
            if reference is not None:
                notes.append(f"{reference['total']:,} people, {reference['phone_count']:,} phones")
            plain_cell = "        -" if shape_label in OVERLAY_ONLY else f"{plain_ms:>8.1f}ms"
            ratio_cell = "       -" if shape_label in OVERLAY_ONLY else f"x{ratio:>7.2f}"
            print(f"  {shape_label:<26} {overlay_ms:>9.1f}ms {plain_cell} "
                  f"{ratio_cell}   {'   '.join(notes)}")
