"""Round 2: can the view-to-view join be fixed with query/view/table changes only?

Round 1 established: the appendrel has no parent statistics, so the join is
estimated ~32,000x over; LIMIT then turns that into a nested loop that grinds
116,723,991 rows. Without LIMIT the same query picks a hash join and takes
123ms. So the fix has to stop LIMIT's tuple fraction from selecting the plan,
or avoid the join entirely.
"""

import time

import pytest
from django.db import connection

from tests.probe_uuid7_scale import load


pytestmark = pytest.mark.django_db(transaction=True)

CUST = "widecustomer_u7_view"
ORDER = "wideorder_u7_view"
CUST_PLAIN = "bu7_customer"
ORDER_PLAIN = "bu7_order"
W = "c.city = 'city42'"


def sql(s):
    with connection.cursor() as c:
        c.execute(s)


def run(q, rounds=2):
    best, rows = None, 0
    for _ in range(rounds):
        started = time.perf_counter()
        with connection.cursor() as c:
            c.execute(q)
            rows = len(c.fetchall())
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return best, rows


def plan_of(q):
    with connection.cursor() as c:
        c.execute("EXPLAIN " + q)
        lines = [r[0] for r in c.fetchall()]
    for line in lines:
        s = line.strip().lstrip("-> ").strip()
        for kind in ("Nested Loop", "Hash Join", "Merge Join"):
            if s.startswith(kind):
                return kind
    return "-"


def report(label, q, note=""):
    try:
        sql("SET statement_timeout = 40000")
        ms, rows = run(q)
        sql("SET statement_timeout = 0")
        print(f"  {label:<54} {ms:>9.1f}ms  {plan_of(q):<12} rows={rows:<4} {note}")
        return ms
    except Exception as exc:  # noqa: BLE001 - a timeout is a result here
        sql("SET statement_timeout = 0")
        print(f"  {label:<54} {'>40s':>9}   {type(exc).__name__:<12} {note}")
        return None


def test_join_fixes():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s\n")

    join = f"SELECT o.* FROM {ORDER} o JOIN {CUST} c ON c.id = o.customer_id WHERE {W}"

    print("=" * 112)
    print("BASELINES")
    print("=" * 112)
    plain = report(
        "plain JOIN plain LIMIT 50",
        f"SELECT o.* FROM {ORDER_PLAIN} o JOIN {CUST_PLAIN} c ON c.id=o.customer_id WHERE {W} LIMIT 50",
    )
    naive = report("view JOIN view LIMIT 50  (what the ORM emits today)", f"{join} LIMIT 50")
    report("view JOIN view, no LIMIT", join)

    print("\n" + "=" * 112)
    print("CANDIDATE FIXES — query level")
    print("=" * 112)
    fixes = {}
    fixes["CTE MATERIALIZED fence, LIMIT outside"] = report(
        "WITH x AS MATERIALIZED (join) SELECT * FROM x LIMIT 50",
        f"WITH x AS MATERIALIZED ({join}) SELECT * FROM x LIMIT 50",
    )
    fixes["= ANY (ARRAY(subquery))"] = report(
        "o.customer_id = ANY (ARRAY(SELECT id FROM view WHERE …))",
        f"SELECT o.* FROM {ORDER} o WHERE o.customer_id = ANY (ARRAY(SELECT c.id FROM {CUST} c WHERE {W})) LIMIT 50",
    )
    fixes["IN (subquery)"] = report(
        "o.customer_id IN (SELECT id FROM view WHERE …)",
        f"SELECT o.* FROM {ORDER} o WHERE o.customer_id IN (SELECT c.id FROM {CUST} c WHERE {W}) LIMIT 50",
    )
    fixes["CTE MATERIALIZED for the id list only"] = report(
        "WITH ids AS MATERIALIZED (SELECT id …) SELECT … IN ids",
        f"WITH ids AS MATERIALIZED (SELECT c.id FROM {CUST} c WHERE {W}) "
        f"SELECT o.* FROM {ORDER} o WHERE o.customer_id IN (SELECT id FROM ids) LIMIT 50",
    )
    fixes["LATERAL"] = report(
        "FROM view c, LATERAL (SELECT … FROM view o WHERE o.customer_id=c.id)",
        f"SELECT o.* FROM {CUST} c, LATERAL (SELECT * FROM {ORDER} o WHERE o.customer_id = c.id) o WHERE {W} LIMIT 50",
    )

    print("\n  the realistic pagination shape — ORDER BY as well as LIMIT")
    report("view JOIN view ORDER BY o.id LIMIT 50", f"{join} ORDER BY o.id LIMIT 50")
    report(
        "= ANY (ARRAY(…)) ORDER BY o.id LIMIT 50",
        f"SELECT o.* FROM {ORDER} o WHERE o.customer_id = ANY "
        f"(ARRAY(SELECT c.id FROM {CUST} c WHERE {W})) ORDER BY o.id LIMIT 50",
    )

    print("\n" + "=" * 112)
    print("CANDIDATE FIXES — table level")
    print("=" * 112)
    sql(f"CREATE MATERIALIZED VIEW IF NOT EXISTS mv_cust AS SELECT * FROM {CUST}")
    sql("CREATE UNIQUE INDEX IF NOT EXISTS mv_cust_pk ON mv_cust (id)")
    sql("CREATE INDEX IF NOT EXISTS mv_cust_city ON mv_cust (city)")
    sql("ANALYZE mv_cust")
    report(
        "MATERIALIZED VIEW of the overlay, joined to the view",
        f"SELECT o.* FROM {ORDER} o JOIN mv_cust c ON c.id=o.customer_id WHERE {W} LIMIT 50",
        note="(stale; upper bound)",
    )

    print("\n" + "=" * 112)
    naive_text = f"{naive:.1f}ms" if naive else ">40s"
    print(f"SUMMARY  (plain baseline = {plain:.1f}ms, naive view join = {naive_text})")
    print("=" * 112)
    for label, ms in sorted((k, v) for k, v in fixes.items() if v is not None):
        speedup = f"{naive / ms:>8.0f}x faster than naive" if naive else ""
        print(f"  {label:<54} {ms:>9.1f}ms  {speedup}")

    sql("DROP MATERIALIZED VIEW IF EXISTS mv_cust")
