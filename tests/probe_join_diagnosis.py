"""Why Postgres will not plan a join between two overlay views — TODO/06.

Diagnostic only. Nothing here is a proposed change to the library.
"""

import re
import time

import pytest
from django.db import connection

from tests.probe_uuid7_scale import load


pytestmark = pytest.mark.django_db(transaction=True)

CUST = "widecustomer_u7_view"
ORDER = "wideorder_u7_view"
CUST_PLAIN = "bu7_customer"
ORDER_PLAIN = "bu7_order"


def sql(s):
    with connection.cursor() as c:
        c.execute(s)


def explain(q, analyze=True):
    prefix = "EXPLAIN (ANALYZE, BUFFERS)" if analyze else "EXPLAIN"
    with connection.cursor() as c:
        c.execute(f"{prefix} {q}")
        return [r[0] for r in c.fetchall()]


def top_join(lines):
    """(node, estimated_rows, actual_rows) for the outermost join node."""
    for line in lines:
        s = line.strip().lstrip("-> ").strip()
        for kind in ("Nested Loop", "Hash Join", "Merge Join", "Hash Anti Join", "Merge Anti Join"):
            if s.startswith(kind):
                est = re.search(r"rows=(\d+)", line)
                act = re.search(r"actual time=[\d.]+\.\.[\d.]+ rows=(\d+)", line)
                return kind, int(est.group(1)) if est else -1, int(act.group(1)) if act else -1
    return "-", -1, -1


def exec_ms(lines):
    for line in lines:
        if "Execution Time" in line:
            return float(line.split(":")[1].strip().split()[0])
    return float("nan")


def report(label, q, analyze=True):
    lines = explain(q, analyze)
    kind, est, act = top_join(lines)
    if analyze:
        ratio = f"{est / max(act, 1):>9,.0f}x" if act >= 0 and est >= 0 else "-"
        print(f"  {label:<52} {exec_ms(lines):>9.1f}ms  {kind:<16} est={est:>12,} act={act:>9,} over={ratio}")
    else:
        print(f"  {label:<52} {'':>11}  {kind:<16} est={est:>12,}")
    return lines


def test_join_diagnosis():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s")
    rows = "SELECT count(*) FROM " + CUST
    with connection.cursor() as c:
        c.execute(rows)
        print(f"{CUST}: {c.fetchone()[0]:,} rows\n")

    W = "WHERE c.city = 'city42'"
    print("=" * 118)
    print("H1 — is the join cardinality estimate blind when BOTH sides are views?")
    print("=" * 118)
    report(
        "plain JOIN plain", f"SELECT o.* FROM {ORDER_PLAIN} o JOIN {CUST_PLAIN} c ON c.id=o.customer_id {W} LIMIT 50"
    )
    report("plain JOIN view ", f"SELECT o.* FROM {ORDER_PLAIN} o JOIN {CUST} c ON c.id=o.customer_id {W} LIMIT 50")
    report("view  JOIN plain", f"SELECT o.* FROM {ORDER} o JOIN {CUST_PLAIN} c ON c.id=o.customer_id {W} LIMIT 50")
    report("view  JOIN view ", f"SELECT o.* FROM {ORDER} o JOIN {CUST} c ON c.id=o.customer_id {W} LIMIT 50")

    print("\n  unfiltered count(*) join — pure cardinality, no LIMIT, no predicate")
    report("plain x plain", f"SELECT count(*) FROM {ORDER_PLAIN} o JOIN {CUST_PLAIN} c ON c.id=o.customer_id")
    report("view  x view ", f"SELECT count(*) FROM {ORDER} o JOIN {CUST} c ON c.id=o.customer_id")

    print("\n" + "=" * 118)
    print("H2 — is it UNION ALL alone, with no anti-join anywhere?")
    print("=" * 118)
    sql(f"CREATE TABLE IF NOT EXISTS empty_cust (LIKE {CUST_PLAIN})")
    sql(f"CREATE TABLE IF NOT EXISTS empty_order (LIKE {ORDER_PLAIN})")
    sql("ANALYZE empty_cust")
    sql("ANALYZE empty_order")
    sql(f"CREATE OR REPLACE VIEW plain_union_cust AS SELECT * FROM {CUST_PLAIN} UNION ALL SELECT * FROM empty_cust")
    sql(f"CREATE OR REPLACE VIEW plain_union_order AS SELECT * FROM {ORDER_PLAIN} UNION ALL SELECT * FROM empty_order")
    report(
        "UNION ALL view JOIN UNION ALL view (no anti-join at all)",
        f"SELECT o.* FROM plain_union_order o JOIN plain_union_cust c ON c.id=o.customer_id {W} LIMIT 50",
    )
    report(
        "UNION ALL view JOIN plain",
        f"SELECT o.* FROM plain_union_order o JOIN {CUST_PLAIN} c ON c.id=o.customer_id {W} LIMIT 50",
    )

    print("\n" + "=" * 118)
    print("H4 — is LIMIT the amplifier?")
    print("=" * 118)
    base = f"SELECT o.* FROM {ORDER} o JOIN {CUST} c ON c.id=o.customer_id {W}"
    report("view JOIN view  LIMIT 50", f"{base} LIMIT 50")
    report("view JOIN view  LIMIT 100000", f"{base} LIMIT 100000")
    report("view JOIN view  no LIMIT", base)

    print("\n" + "=" * 118)
    print("H3 — can the view be an index-driven inner side (parameterized path)?")
    print("=" * 118)
    with connection.cursor() as c:
        c.execute(f"SELECT id FROM {CUST_PLAIN} LIMIT 3")
        ids = [r[0] for r in c.fetchall()]
    values = ", ".join(f"('{i}'::uuid)" for i in ids)
    report("3 literal ids JOIN view", f"SELECT v.* FROM (VALUES {values}) t(id) JOIN {CUST} v ON v.id = t.id")
    report("3 literal ids JOIN plain", f"SELECT v.* FROM (VALUES {values}) t(id) JOIN {CUST_PLAIN} v ON v.id = t.id")

    print("\n" + "=" * 118)
    print("CANDIDATE REWRITES — query/view/table level only, no server settings")
    print("=" * 118)
    report(
        "IN (subquery)   semi-join instead of join",
        f"SELECT o.* FROM {ORDER} o WHERE o.customer_id IN "
        f"(SELECT c.id FROM {CUST} c WHERE c.city = 'city42') LIMIT 50",
    )
    report(
        "EXISTS          correlated semi-join",
        f"SELECT o.* FROM {ORDER} o WHERE EXISTS "
        f"(SELECT 1 FROM {CUST} c WHERE c.id = o.customer_id AND c.city = 'city42') LIMIT 50",
    )
    report(
        "= ANY (ARRAY(…)) forces the id list to materialise first",
        f"SELECT o.* FROM {ORDER} o WHERE o.customer_id = ANY "
        f"(ARRAY(SELECT c.id FROM {CUST} c WHERE c.city = 'city42')) LIMIT 50",
    )

    print("\n  --- diagnostic ceilings (NOT proposed fixes: these are server settings)")
    for knob in ("enable_nestloop = off", "enable_material = off"):
        sql(f"SET {knob}")
        report(f"view JOIN view with {knob}", f"{base} LIMIT 50")
        sql("RESET ALL")

    print("\n" + "=" * 118)
    print("full plan: view JOIN view LIMIT 50")
    print("=" * 118)
    for line in explain(f"{base} LIMIT 50")[:26]:
        print("   ", line[:160])

    sql("DROP VIEW IF EXISTS plain_union_cust")
    sql("DROP VIEW IF EXISTS plain_union_order")
    sql("DROP TABLE IF EXISTS empty_cust")
    sql("DROP TABLE IF EXISTS empty_order")
