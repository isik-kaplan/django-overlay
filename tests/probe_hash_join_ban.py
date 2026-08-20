"""What DJANGO_OVERLAY_FORCE_HASH_JOINS is worth, and what it costs.

The ban is on by default, so it needs to answer for itself on both sides:

  * on the shapes it exists for -- two or more m2m hops, where the planner
    estimates 266,974,515,000 rows for a 132-row answer and picks a nested
    loop that runs to exhaustion;
  * on the shapes it deliberately stays out of. One m2m hop is already
    0.9x-2.3x a plain table, so if banning nested loops there is free, the
    threshold is over-cautious; if it is expensive, the threshold is load
    bearing. Either way the number should be written down rather than assumed,
    so the last section forces the ban below its own threshold to find out.

Three columns throughout: the ban off, the ban on, and a plain non-overlay
table holding identical rows. The plain column is the floor -- not something
the overlay can reach, but the thing that says whether a ratio is the view's
fault or the query's.

    OVERLAY_BENCH_SCALE=1.0 POSTGRES_USER=postgres uv run pytest \\
        --reuse-db tests/probe_hash_join_ban.py -s -q -o addopts="" --no-cov
"""

import os
import time
from contextlib import contextmanager

import pytest
from django.db import OperationalError, connection
from django.db.models import Count
from django.test import override_settings

from django_overlay import models as overlay_models
from tests.probe_bench_graph import load
from tests.testapp.models import BenchPerson, PlainPerson


pytestmark = pytest.mark.django_db(transaction=True)

# Env-configurable so a scale sweep can widen the cap where the honest number
# is above it, and CI can narrow it -- there, "did this cell cap" is the whole
# signal and waiting out the exact figure is dead time.
CAP_MS = int(os.environ.get("OVERLAY_BAN_CAP_MS", 30_000))
PASSES = int(os.environ.get("OVERLAY_BAN_PASSES", 2))
OFF = override_settings(DJANGO_OVERLAY_FORCE_HASH_JOINS=False)

NARROW = {"addresses__city": "city0"}
BROAD = {"phones__kind": "mobile"}
BROADEST = {"addresses__country": "US"}
THIRD = {"emails__domain": "example.com"}
SCALAR = {"city__in": [f"city{n}" for n in range(25)]}
PLAIN_HOP = {"labels__kind": "volunteer"}


def cap(milliseconds):
    with connection.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = {milliseconds}")
        cursor.execute("SET lock_timeout = 5000")


@contextmanager
def threshold(value):
    """Lower the hop threshold so the ban applies where it normally would not.

    Only for the last section, which measures what the ban costs on the shapes
    the threshold is there to protect.
    """
    previous = overlay_models._HASH_JOIN_THRESHOLD
    overlay_models._HASH_JOIN_THRESHOLD = value
    try:
        yield
    finally:
        overlay_models._HASH_JOIN_THRESHOLD = previous


def resolve(model, scope):
    return model.objects.filter(**scope).values("pk").distinct().count()


def page(model, scope):
    """An ordered page -- the shape where the LIMIT collapses the tuple
    fraction and the nested loop looks free."""
    return len(list(model.objects.filter(**scope).order_by("id")[:200]))


def summarise(model, scope):
    """The relation counts from a summary panel, over a joined scope."""
    return model.objects.filter(**scope).aggregate(
        total=Count("id", distinct=True),
        phones=Count("phones", distinct=True),
    )["total"]


def scoped_subquery(model, scope):
    """The scope attached as a subquery rather than filtered inline.

    The joins move out of the outer alias map and into the subquery, which is
    what made the first version of the detection miss this shape entirely.
    """
    return model.objects.filter(pk__in=model.objects.filter(**scope).values("pk")).count()


def leaf_by_leaf(model, scope):
    """Each condition resolved as its own subquery and chained.

    This shape already worked before the ban -- no two m2m joins ever share a
    plan -- so the question here is not whether the ban rescues it but whether
    it leaves it alone. Now that subqueries are counted, it trips the
    threshold, and a rescue that slows down the thing that already worked is
    not a rescue.
    """
    queryset = model.objects.all()
    for field, value in scope.items():
        queryset = queryset.filter(pk__in=model.objects.filter(**{field: value}).values("pk"))
    return queryset.values("pk").distinct().count()


def timed(build, rounds=3):
    """(best milliseconds, value) after a discarded warm-up.

    The warm-up is not politeness, it is the difference between measuring the
    feature and measuring the page cache. An earlier version took best-of-two
    with no warm-up and produced a table that contradicted its own previous
    run: a query with no joins at all, unbanned in both, moved from 13ms to
    231ms between runs. Nothing under test can do that.
    """
    try:
        build()
    except OperationalError:
        return float(CAP_MS), None
    best, value = None, None
    for _ in range(rounds):
        started = time.perf_counter()
        try:
            value = build()
        except OperationalError:
            return float(CAP_MS), None
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
        if best > 5_000:
            break
    return best, value


def assert_setting_is_clean():
    """Every cell starts from the same session state.

    If a restore ever failed, later "ban off" cells would silently run banned
    and the comparison would quietly become meaningless rather than wrong in a
    visible way.
    """
    with connection.cursor() as cursor:
        cursor.execute("SHOW enable_nestloop")
        assert cursor.fetchone()[0] == "on", "a previous cell leaked the ban"


def row(label, operation, scope, hops):
    assert_setting_is_clean()
    with OFF:
        unbanned_ms, unbanned = timed(lambda: operation(BenchPerson, scope))
    banned_ms, banned = timed(lambda: operation(BenchPerson, scope))
    plain_ms, plain = timed(lambda: operation(PlainPerson, scope))

    if None not in (unbanned, banned):
        assert unbanned == banned, f"{label}: the ban changed the result"
    if None not in (banned, plain):
        assert banned == plain, f"{label}: overlay and plain disagree"

    def cell(milliseconds, value):
        return f"{'>' + str(CAP_MS // 1000) + 's':>10}" if value is None else f"{milliseconds:>8.0f}ms"

    if None in (unbanned, banned):
        gain = "     -"
    else:
        gain = f"x{unbanned_ms / banned_ms:>5.1f}" if banned_ms else "     -"
    found = "capped" if banned is None else f"{banned:,}"
    print(f"  {label:<34} {hops:>4} {cell(unbanned_ms, unbanned)} {cell(banned_ms, banned)} "
          f"{gain} {cell(plain_ms, plain):>10}   {found}")


def header(title):
    print("\n" + "=" * 104)
    print(title)
    print("=" * 104)
    print(f"  {'query':<34} {'hops':>4} {'ban off':>10} {'ban on':>10} {'gain':>6} {'plain':>10}   rows")
    print("  " + "-" * 96)


def test_hash_join_ban():
    load()
    cap(CAP_MS)
    print(f"\n\n  {BenchPerson.objects.count():,} people")

    # Twice, because the first version of this probe contradicted its own
    # previous run and there was no way to tell signal from drift. Anything
    # that moves between passes is drift; the control row below says how much
    # drift to expect.
    for attempt in range(1, PASSES + 1):
        header(f"PASS {attempt}: TWO OR MORE HOPS (what the ban is for)")
        row("two hops, narrow + broad", resolve, NARROW | BROAD, 2)
        row("two hops + a scalar scope", resolve, NARROW | BROAD | SCALAR, 2)
        row("two hops, ordered page", page, NARROW | BROAD, 2)
        row("two hops, summary counts", summarise, NARROW | BROAD, 2)
        row("two hops, scope as subquery", scoped_subquery, NARROW | BROAD, 2)
        row("two leaves, chained subqueries", leaf_by_leaf, NARROW | BROAD, 2)

        header(f"PASS {attempt}: ONE HOP (only the paged one is banned)")
        row("one hop, narrow", resolve, NARROW, 1)
        row("one hop, broad", resolve, BROAD, 1)
        row("one hop, ordered page", page, BROAD, 1)

        header(f"PASS {attempt}: NEVER BANNED (the noise floor)")
        # Neither column applies the ban, so the two should agree. Whatever gap
        # shows up here is what the harness cannot measure below, and no gain
        # in the tables above is meaningful unless it clears it.
        row("view -> plain table", resolve, PLAIN_HOP, 1)
        row("no join at all", resolve, SCALAR, 0)

    print("\n" + "=" * 104)
    print("WHAT THE EXCLUSIONS WOULD COST IF BANNED ANYWAY (threshold forced to 1)")
    print("=" * 104)
    print(f"  {'query':<34} {'hops':>4} {'ban off':>10} {'ban on':>10} {'gain':>6} {'plain':>10}   rows")
    print("  " + "-" * 96)
    with threshold(1):
        # The two the no-LIMIT threshold of 4 deliberately excludes. At 1,000,000
        # rows banning them was free, which argued for collapsing to a single
        # threshold; the question is whether that still holds once the hash the
        # ban forces is built over a larger relation.
        row("one hop, narrow", resolve, NARROW, 1)
        row("one hop, broad", resolve, BROAD, 1)
        row("view -> plain table", resolve, PLAIN_HOP, 1)
        # No user-written join at all -- but the view itself contains one, the
        # NOT EXISTS anti-join against the base table, so there is still a plan
        # here for the ban to change.
        row("no join at all", resolve, SCALAR, 0)
