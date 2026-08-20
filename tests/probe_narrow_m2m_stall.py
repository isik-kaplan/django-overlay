"""132 matching people, 84ms on a plain table, over four minutes on the view.

probe_selectivity_sweep set out to show that the earlier pessimistic numbers
were an artifact of filtering on a four-valued column, and found the opposite:
the *narrowest* condition in the sweep was the one that stalled. A 2,850x gap
on a result set of 132 rows is not a per-row cost, it is a plan going wrong,
and it contradicts the earlier 300,000-person run where a narrow third
condition finished in 353ms.

Three candidate explanations, which this separates:

  1. the fence does not fire for an `__in` tail, so this query is simply
     unfenced -- every earlier success used an `exact` tail;
  2. the fence fires but the *inner* subquery it builds is itself unplannable
     at this selectivity;
  3. nothing to do with the fence: two m2m joins with no LIMIT is the bad
     shape, and 300k was just small enough to hide it.

So: compile each variant and report whether `= ANY (ARRAY` is in the SQL, what
the planner estimates against what is really there, and how long it takes with
a short cap. EXPLAIN without ANALYZE for the shapes that cannot finish -- the
estimate is the interesting half anyway.

    OVERLAY_BENCH_SCALE=1.0 POSTGRES_USER=postgres uv run pytest \\
        --reuse-db tests/probe_narrow_m2m_stall.py -s -q -o addopts="" --no-cov
"""

import time

import pytest
from django.db import OperationalError, connection
from django.test import override_settings

from tests.probe_bench_graph import load
from tests.testapp.models import BenchPerson, PlainPerson


pytestmark = pytest.mark.django_db(transaction=True)

OFF = override_settings(DJANGO_OVERLAY_REWRITE_TRAVERSALS=False)
CAP_MS = 20_000

PHONES = {"phones__kind": "mobile"}

VARIANTS = (
    ("city__in=[city0]  + phones", {"addresses__city__in": ["city0"]} | PHONES),
    ("city=city0        + phones", {"addresses__city": "city0"} | PHONES),
    ("city__in=[city0]  alone", {"addresses__city__in": ["city0"]}),
    ("city=city0        alone", {"addresses__city": "city0"}),
    ("phones            alone", dict(PHONES)),
)


def cap(milliseconds):
    with connection.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = {milliseconds}")
        cursor.execute("SET lock_timeout = 5000")


def fence_fired(scope):
    statement, _ = BenchPerson.objects.filter(**scope).query.sql_with_params()
    return "= ANY (ARRAY" in statement


def estimate(model, scope):
    """(top node, estimated rows) from EXPLAIN without ANALYZE.

    No ANALYZE: the whole point is the shapes that cannot finish, and the
    estimate is what went wrong in the first place.
    """
    statement, params = model.objects.filter(**scope).values("pk").query.sql_with_params()
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN (FORMAT JSON) {statement}", params)
        root = cursor.fetchone()[0][0]["Plan"]
    return root["Node Type"], root["Plan Rows"]


def timed(build):
    started = time.perf_counter()
    try:
        value = build()
    except OperationalError:
        return float(CAP_MS), None
    return (time.perf_counter() - started) * 1000, value


def resolve(model, scope):
    return len(set(model.objects.filter(**scope).values_list("pk", flat=True)))


def test_narrow_m2m_stall():
    load()
    cap(CAP_MS)

    print("\n\n" + "=" * 108)
    print("DOES THE FENCE FIRE, AND WHAT DOES THE PLANNER THINK?")
    print("=" * 108)
    print(f"  {'variant':<32} {'fenced':>7} {'est rows':>12} {'top node':<22} {'overlay':>10} {'plain':>9}")
    print("  " + "-" * 100)

    truth = {}
    for label, scope in VARIANTS:
        fenced = fence_fired(scope)
        node, rows = estimate(BenchPerson, scope)
        overlay_ms, overlay_people = timed(lambda s=scope: resolve(BenchPerson, s))
        plain_ms, plain_people = timed(lambda s=scope: resolve(PlainPerson, s))
        truth[label] = (overlay_people, plain_people)
        overlay_cell = f"{'>' + str(CAP_MS // 1000) + 's':>10}" if overlay_people is None \
            else f"{overlay_ms:>8.0f}ms"
        print(f"  {label:<32} {'yes' if fenced else 'NO':>7} {rows:>12,} {node:<22} "
              f"{overlay_cell} {plain_ms:>7.0f}ms")

    print("\n" + "=" * 108)
    print("HOW MANY ROWS ARE REALLY THERE (plain table, ground truth)")
    print("=" * 108)
    for label, (overlay_people, plain_people) in truth.items():
        agreement = "" if overlay_people in (None, plain_people) else "   ROWS DIFFER"
        found = "capped" if overlay_people is None else f"{overlay_people:,}"
        print(f"  {label:<32} plain {plain_people:>9,}   overlay {found:>9}{agreement}")

    print("\n" + "=" * 108)
    print("IS THE FENCE HELPING OR HURTING HERE?")
    print("=" * 108)
    for label, scope in VARIANTS:
        with OFF:
            unfenced_ms, unfenced_people = timed(lambda s=scope: resolve(BenchPerson, s))
        fenced_ms, fenced_people = timed(lambda s=scope: resolve(BenchPerson, s))
        if unfenced_people is not None and fenced_people is not None:
            assert unfenced_people == fenced_people, f"{label}: the fence changed the result"
        cells = " ".join(
            f"{'>' + str(CAP_MS // 1000) + 's':>10}" if people is None else f"{milliseconds:>8.0f}ms"
            for milliseconds, people in ((unfenced_ms, unfenced_people), (fenced_ms, fenced_people))
        )
        print(f"  {label:<32} unfenced/fenced {cells}")
