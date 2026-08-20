"""Does the ban still hold at the depth a real saved search reaches?

probe_hash_join_ban established two hops thoroughly: 27,637ms -> 405ms, two
passes, 1.0x noise floor. It says nothing about three or four, and that is the
depth the application actually runs -- a saved search is a boolean tree of
several conditions, not a pair.

The distinction is not pedantry. The m2m fence worked at one hop and did not
compose to two: each fence handed the planner an InitPlan for its own subquery
and nothing supplied a joint selectivity, so two blind estimates multiplied
into 266,974,515,000. Assuming the ban composes because it works at two hops
would be the same error a second time. The one measurement ever taken of three
hops was 29,261ms *with* the ban, under an older threshold and before
get_aggregation() was hooked -- not a fair test, but not nothing either.

So: sweep the hop count and watch the shape of the curve. Linear is fine.
Anything accelerating means the ban buys depth rather than fixing it, and the
useful number becomes how many hops it buys.

Both the plain resolve and the ordered page, because the LIMIT is what makes
the planner reach for a nested loop in the first place and the two thresholds
treat them differently.

    OVERLAY_BENCH_SCALE=1.0 POSTGRES_USER=postgres uv run pytest \\
        --reuse-db tests/probe_hop_scaling.py -s -q -o addopts="" --no-cov
"""

import time

import pytest
from django.db import OperationalError, connection
from django.test import override_settings

from tests.probe_bench_graph import load
from tests.testapp.models import BenchPerson, PlainPerson


pytestmark = pytest.mark.django_db(transaction=True)

CAP_MS = 60_000
OFF = override_settings(DJANGO_OVERLAY_FORCE_HASH_JOINS=False)

# Ordered narrow-to-broad, so each added hop is a further restriction rather
# than a new way to multiply rows. The fourth is the tenant-owned plain table:
# a real saved search mixes vendor-sourced conditions with tenant-only ones,
# and by then the query has four relations regardless of what backs them.
HOPS = (
    ("addresses__city", "city0"),
    ("phones__kind", "mobile"),
    ("emails__domain", "example.com"),
    ("labels__kind", "volunteer"),
)


def cap(milliseconds):
    with connection.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = {milliseconds}")
        cursor.execute("SET lock_timeout = 5000")


def assert_setting_is_clean():
    with connection.cursor() as cursor:
        cursor.execute("SHOW enable_nestloop")
        assert cursor.fetchone()[0] == "on", "a previous cell leaked the ban"


def resolve(model, scope):
    return model.objects.filter(**scope).values("pk").distinct().count()


def page(model, scope):
    return len(list(model.objects.filter(**scope).order_by("id")[:200]))


def timed(build, rounds=2):
    """Best of `rounds` after a discarded warm-up. See probe_hash_join_ban."""
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


def sweep(label, operation):
    print("\n" + "=" * 100)
    print(label)
    print("=" * 100)
    print(f"  {'hops':>4} {'conditions':<44} {'ban off':>10} {'ban on':>10} {'gain':>6} {'plain':>9}   rows")
    print("  " + "-" * 92)

    scope = {}
    for depth, (field, value) in enumerate(HOPS, start=1):
        scope = scope | {field: value}
        assert_setting_is_clean()
        with OFF:
            unbanned_ms, unbanned = timed(lambda s=scope: operation(BenchPerson, s))
        banned_ms, banned = timed(lambda s=scope: operation(BenchPerson, s))
        plain_ms, plain = timed(lambda s=scope: operation(PlainPerson, s))

        if None not in (unbanned, banned):
            assert unbanned == banned, f"{depth} hops: the ban changed the result"
        if None not in (banned, plain):
            assert banned == plain, f"{depth} hops: overlay and plain disagree"

        def cell(milliseconds, found):
            return f"{'>' + str(CAP_MS // 1000) + 's':>10}" if found is None else f"{milliseconds:>8.0f}ms"

        gain = "     -"
        if None not in (unbanned, banned) and banned_ms:
            gain = f"x{unbanned_ms / banned_ms:>5.1f}"
        rows = "capped" if banned is None else f"{banned:,}"
        print(f"  {depth:>4} {', '.join(scope):<44} {cell(unbanned_ms, unbanned)} "
              f"{cell(banned_ms, banned)} {gain} {cell(plain_ms, plain):>9}   {rows}")


def test_hop_scaling():
    load()
    cap(CAP_MS)
    print(f"\n\n  {BenchPerson.objects.count():,} people")
    sweep("RESOLVE: how many people match (no LIMIT)", resolve)
    sweep("ORDERED PAGE: the first 200 by id (LIMIT)", page)
