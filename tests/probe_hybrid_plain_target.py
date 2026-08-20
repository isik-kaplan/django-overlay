"""Does the collapse go away if you only overlay what actually needs it?

Everything measured so far joins two overlay views, and that shape does not
survive two conditions: probe_narrow_m2m_stall found the planner estimating
267,425,037,000 rows for a 132-row answer, because an appendrel parent carries
no statistics and nothing supplies a joint selectivity for two of them.

But not every entity needs to be a view. A view exists to merge a tenant row
over a vendor row -- and a label, a saved list, a campaign has no vendor row to
merge. Those are tenant-owned outright, and `docs/reference/QUERY_REWRITING.md`
records that a traversal whose target is a *plain* table costs 1.2-1.3x,
because the plain side's statistics rescue the estimate. `_m2m_fence()` agrees
by construction: it requires both the through model and the target to be view
models, and declines otherwise.

So the question this settles is whether a normalized schema is viable on the
overlay provided only the vendor-sourced entities go behind views:

    view -> view    addresses__city   (BenchAddress has a source)
    view -> plain   labels__kind      (BenchLabel does not)

Four conditions, in the four combinations that matter. If mixing one of each
finishes while two view->view conditions do not, the rule is "overlay only what
has a vendor source" and normalized schemas are back on the table. If mixing
collapses too, one view->view hop anywhere in a query is enough to poison it,
and the answer stays no.

    OVERLAY_BENCH_SCALE=1.0 OVERLAY_BENCH_REBUILD=1 POSTGRES_USER=postgres \\
        uv run pytest --reuse-db tests/probe_hybrid_plain_target.py \\
        -s -q -o addopts="" --no-cov
"""

import time

import pytest
from django.db import OperationalError, connection

from tests.probe_bench_graph import load
from tests.testapp.models import BenchLabel, BenchPerson, PlainPerson


pytestmark = pytest.mark.django_db(transaction=True)

CAP_MS = 20_000

# Two selectivities on each side, because selectivity has mattered everywhere
# else: `kind` has four values and `name` has two hundred, the same spread as
# `country` against `city` on the view side.
VIEW_NARROW = {"addresses__city": "city0"}
# A single city reaches 200 of 1,000,000 people, which is too narrow to
# combine with anything: intersected with a 0.4% label it expects less than one
# person, and an empty result times how fast Postgres finds nothing. The
# combination cases use a hundred cities (~2%) so there is something to find.
VIEW_MID = {"addresses__city__in": [f"city{n}" for n in range(100)]}
VIEW_BROAD = {"phones__kind": "mobile"}
PLAIN_NARROW = {"labels__name": "label7"}
PLAIN_BROAD = {"labels__kind": "volunteer"}

CASES = (
    ("plain alone, narrow        (view->plain)", [PLAIN_NARROW]),
    ("plain alone, broad         (view->plain)", [PLAIN_BROAD]),
    ("view alone, narrow         (view->view)", [VIEW_NARROW]),
    ("view alone, mid            (view->view)", [VIEW_MID]),
    ("plain + plain              (both plain)", [PLAIN_NARROW, PLAIN_BROAD]),
    ("view + plain, narrow plain <- the question", [VIEW_MID, PLAIN_NARROW]),
    ("view + plain, broad plain  <- the question", [VIEW_MID, PLAIN_BROAD]),
    ("view + view                (the known-bad)", [VIEW_MID, VIEW_BROAD]),
    ("view + view + plain", [VIEW_MID, VIEW_BROAD, PLAIN_NARROW]),
)


def cap(milliseconds):
    with connection.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = {milliseconds}")
        cursor.execute("SET lock_timeout = 5000")


def naive(model, conditions):
    """One filter, the way anyone would write it."""
    combined = {}
    for condition in conditions:
        combined |= condition
    return model.objects.filter(**combined).values("pk").distinct().count()


def estimate(model, conditions):
    """What the planner thinks it will produce, from EXPLAIN without ANALYZE.

    The estimate is the whole story on these shapes -- a plan that never
    finishes still explains instantly, and the number it explains with is the
    reason it never finishes.
    """
    combined = {}
    for condition in conditions:
        combined |= condition
    statement, params = model.objects.filter(**combined).values("pk").query.sql_with_params()
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN (FORMAT JSON) {statement}", params)
        return cursor.fetchone()[0][0]["Plan"]["Plan Rows"]


def fenced(conditions):
    combined = {}
    for condition in conditions:
        combined |= condition
    statement, _ = BenchPerson.objects.filter(**combined).query.sql_with_params()
    return "= ANY (ARRAY" in statement


def timed(build):
    """(milliseconds, value), or (cap, None) on timeout.

    `build()` is called on its own line on purpose. Returning
    `(perf_counter() - started) * 1000, build()` reads fine and is wrong:
    tuple elements evaluate left to right, so the elapsed time is computed
    before the query runs and every cell reports 0ms.
    """
    started = time.perf_counter()
    try:
        value = build()
    except OperationalError:
        return float(CAP_MS), None
    return (time.perf_counter() - started) * 1000, value


def test_hybrid_plain_target():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s")
    cap(CAP_MS)
    print(f"  {BenchPerson.objects.count():,} people, {BenchLabel.objects.count():,} labels")

    print("\n" + "=" * 108)
    print("ONE HOP TO A PLAIN TABLE VS ONE HOP TO A VIEW")
    print("=" * 108)
    print(f"  {'conditions':<44} {'fenced':>7} {'est rows':>16} {'overlay':>10} {'plain':>9} {'ratio':>8}")
    print("  " + "-" * 100)

    for label, conditions in CASES:
        overlay_ms, overlay_people = timed(lambda c=conditions: naive(BenchPerson, c))
        plain_ms, plain_people = timed(lambda c=conditions: naive(PlainPerson, c))
        rows = estimate(BenchPerson, conditions)

        if overlay_people is None:
            cells = f"{'>' + str(CAP_MS // 1000) + 's':>10} {plain_ms:>7.0f}ms {'':>8}"
        else:
            ratio = overlay_ms / plain_ms if plain_ms else float("nan")
            cells = f"{overlay_ms:>8.0f}ms {plain_ms:>7.0f}ms x{ratio:>7.1f}"
        agreement = ""
        if None not in (overlay_people, plain_people) and overlay_people != plain_people:
            agreement = f"   ROWS DIFFER {overlay_people:,} vs {plain_people:,}"
        found = "capped" if overlay_people is None else f"{overlay_people:,}"
        print(f"  {label:<44} {'yes' if fenced(conditions) else 'no':>7} {rows:>16,} {cells}"
              f"   {found}{agreement}")
