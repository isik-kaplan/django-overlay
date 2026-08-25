"""Several m2m conditions in one filter() — the real application query.

    People.objects.filter(addresses__state="CA", phones__kind=Mobile, lists=LIST_1)

Three things happen at once here, and only one of them is about the overlay:

  1. **Each m2m adds a join, and joins to different relations multiply.** A
     person with 3 matching addresses and 4 matching phones produces 12 rows.
     That is Django semantics on any schema, overlay or not, and it is why this
     shape almost always wants `.distinct()`.
  2. Each m2m adds a fence, so the fences stack too.
  3. The fences are **not correlated with each other or with the outer
     filter** — each materialises its own id set regardless of how selective
     the rest of the query is.

So the question this answers is: does adding a *selective* condition rescue a
query that also has two broad ones?

    OVERLAY_BENCH_SCALE=0.3 POSTGRES_USER=postgres uv run pytest \\
        tests/probe_multi_m2m.py -s -q -o addopts="" --no-cov
"""

import os
import time

import pytest
from django.db import OperationalError, connection
from django.test import override_settings

from benchmark.graph import load
from tests.testapp.models import BenchPerson, PlainPerson


pytestmark = pytest.mark.django_db(transaction=True)

OFF = override_settings(DJANGO_OVERLAY_REWRITE_TRAVERSALS=False)

# Broad conditions, of the kind a real "state = CA" / "kind = mobile" pair is.
#
# 'US' rather than 'GB' deliberately. The link generator picks source-vs-organic
# rows on `g %% 2` and the country on `g %% 4`, and those parities interact:
# every country is exactly a quarter of the address table, but US and DE are
# reachable from 60,000 people while FR and GB reach only 15,000. Benchmarking
# the unlucky half understates every broad shape by 4x. See probe_reachability.
BROAD_A = {"addresses__country": "US"}
BROAD_B = {"phones__kind": "mobile"}
# A scope on the person table itself — the `target_list=LIST_1` of the real
# query, sized like a marketing list rather than a sliver: 25 of 1,000 cities,
# so 2.5% of people. A single city is 0.1%, which intersects the two broad
# conditions down to nothing and times an empty result instead of a real one.
SELECTIVE = {"city__in": [f"city{n}" for n in range(25)]}
# What a genuinely narrow m2m membership looks like.
NARROW = {"addresses__postcode": "pc42"}


TIMEOUT_MS = int(os.environ.get("OVERLAY_M2M_TIMEOUT_MS", 20_000))


def cap_the_session():
    """Bound *every* statement from here on, not just the ones inside `timed()`.

    An earlier version of this probe capped only the measured queries, and the
    unmeasured `.count()` calls that size the conditions ran away instead —
    for over an hour, holding ACCESS SHARE on the bench views, so that the
    `TRUNCATE` at the top of the next run's `load()` queued behind them forever.
    Killing pytest does not cancel a running backend, so the pile-up outlived
    three runs. The `lock_timeout` is the second half of that lesson: a run that
    cannot get its locks should fail in a second, not wait.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = {TIMEOUT_MS}")
        cursor.execute("SET lock_timeout = 5000")


def timed(build, rounds=3, ceiling=8000):
    """(best milliseconds, rows). Returns (timeout, None) if the query is worse
    than the timeout — several shapes here are genuinely unbounded, and a probe
    that hangs measures nothing."""
    best, rows = None, None
    for _ in range(rounds):
        started = time.perf_counter()
        try:
            rows = len(list(build()))
        except OperationalError:
            return float(TIMEOUT_MS), None
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
        if best > ceiling:
            break
    return best, rows


def compare(label, filters, distinct=False, limit=200):
    def build(model):
        qs = model.objects.filter(**filters)
        if distinct:
            qs = qs.distinct()
        return qs.order_by("id")[:limit]

    overlay_ms, overlay_rows = timed(lambda: build(BenchPerson))
    plain_ms, plain_rows = timed(lambda: build(PlainPerson))
    ratio = overlay_ms / plain_ms if plain_ms else float("nan")
    # A timed-out side reports the ceiling and no rows, so neither the row
    # count nor a rows-differ comparison means anything for it.
    if overlay_rows is None or plain_rows is None:
        which = "overlay" if overlay_rows is None else "plain"
        rows = f"  ({which} hit the {TIMEOUT_MS / 1000:.0f}s ceiling)"
    else:
        rows = f"{overlay_rows:>5} rows" + ("" if overlay_rows == plain_rows else "  ROWS DIFFER")
    print(f"  {label:<52} {overlay_ms:>9.1f}ms {plain_ms:>8.1f}ms  x{ratio:>8.2f}  {rows}")


def multiplication(filters, sample=2000):
    """(distinct people, joined rows, factor) over a *sample* of people.

    Counting the full join of two broad m2m conditions is a cartesian product
    over the whole table — tens of millions of rows, and minutes of wall clock
    to count. Restricting to a slice of people measures the same duplication
    factor without that.

    The slice is drawn from the people who *match*, not from the table at
    large. Duplication is a property of matching rows, and sampling the table
    instead means a selective filter leaves nothing in the sample to measure:
    2.5%% of a 2,000-person sample intersected with two broad conditions is
    empty, and an empty sample reports no duplication rather than none found.
    """
    ids = list(BenchPerson.objects.filter(**filters).values_list("pk", flat=True).distinct()[:sample])
    scoped = BenchPerson.objects.filter(pk__in=ids, **filters)
    try:
        joined = scoped.count()
        people = scoped.values("pk").distinct().count()
    except OperationalError:
        return None, None, float("nan")
    return people, joined, (joined / people if people else float("nan"))


def test_multi_m2m():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s")
    cap_the_session()
    total = BenchPerson.objects.count()

    print("\n" + "=" * 108)
    print("HOW MANY PEOPLE DOES EACH CONDITION MATCH?")
    print("=" * 108)
    print("  (exact, over the whole table — a single grouped anti-join is cheap;")
    print("   it was the *sampling* that used to make this look expensive)")
    for label, filters in (
        ("addresses__country='US'", BROAD_A),
        ("phones__kind='mobile'", BROAD_B),
        ("city='city42'", SELECTIVE),
        ("addresses__postcode='pc42'", NARROW),
    ):
        try:
            hits = BenchPerson.objects.filter(**filters).values("pk").distinct().count()
        except OperationalError:
            print(f"  {label:<34} {'timed out':>8}")
            continue
        print(f"  {label:<34} {hits:>8,} people   {100.0 * hits / total:>5.1f}% of {total:,}")

    print("\n" + "=" * 108)
    print("ROW MULTIPLICATION: what the joins do when you stack them")
    print("=" * 108)
    print("  (measured over a 2,000-person slice — counting the full cartesian")
    print("   product of two broad m2m conditions takes minutes)")
    for label, filters in (
        ("one broad m2m", BROAD_A),
        ("two broad m2m", BROAD_A | BROAD_B),
        ("two broad + selective", BROAD_A | BROAD_B | SELECTIVE),
    ):
        people, joined, factor = multiplication(filters)
        if people is None:
            print(f"  {label:<34} {'timed out':>8}")
            continue
        print(f"  {label:<34} {people:>8,} people -> {joined:>9,} rows   x{factor:.1f} duplication")

    print("\n" + "=" * 108)
    print("COST")
    print("=" * 108)
    print(f"  {'query':<52} {'overlay':>11} {'plain':>10}  {'ratio':>9}")
    print("  " + "-" * 100)
    compare("one broad m2m", BROAD_A)
    compare("TWO broad m2m", BROAD_A | BROAD_B)
    compare("two broad + selective   <- the question", BROAD_A | BROAD_B | SELECTIVE)
    compare("two broad + NARROW", BROAD_A | BROAD_B | NARROW)
    compare("narrow alone", NARROW)
    print()
    compare("two broad + selective, .distinct()", BROAD_A | BROAD_B | SELECTIVE, distinct=True)
    compare("two broad + narrow, .distinct()", BROAD_A | BROAD_B | NARROW, distinct=True)

    print("\n" + "=" * 108)
    print("WHAT THE FENCE IS WORTH ON THIS SHAPE")
    print("=" * 108)
    for label, filters in (
        ("two broad m2m", BROAD_A | BROAD_B),
        ("two broad + selective", BROAD_A | BROAD_B | SELECTIVE),
        ("two broad + narrow", BROAD_A | BROAD_B | NARROW),
    ):

        def build(filters=filters):
            return BenchPerson.objects.filter(**filters).order_by("id")[:200]

        with OFF:
            unfenced, unfenced_rows = timed(build)
        fenced, fenced_rows = timed(build)
        # None means that side hit the timeout, so there is nothing to compare.
        if unfenced_rows is not None and fenced_rows is not None:
            assert unfenced_rows == fenced_rows, f"{label}: the fence changed the result"
        timed_out = "  (one side timed out)" if None in (unfenced_rows, fenced_rows) else ""
        print(
            f"  {label:<34} unfenced {unfenced:>9.1f}ms   fenced {fenced:>9.1f}ms   "
            f"x{unfenced / fenced:>6.1f}{timed_out}"
        )

    print("\n" + "=" * 108)
    print("DOES DROPPING THE ORDER BY HELP, AS IT DID FOR THE SINGLE BROAD CASE?")
    print("=" * 108)
    for label, filters in (
        ("two broad m2m", BROAD_A | BROAD_B),
        ("two broad + selective", BROAD_A | BROAD_B | SELECTIVE),
    ):
        ordered, _ = timed(lambda f=filters: BenchPerson.objects.filter(**f).order_by("id")[:200])
        unordered, _ = timed(lambda f=filters: BenchPerson.objects.filter(**f)[:200])
        print(f"  {label:<34} ordered {ordered:>9.1f}ms   unordered {unordered:>9.1f}ms   x{ordered / unordered:>6.1f}")
