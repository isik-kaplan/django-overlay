"""What `OverlayMeta.overridable = False` is actually worth.

Hand-written SQL predicted the win: the same two tables ordered by
id measured 0.031ms as a bare `UNION ALL` against 53ms with an anti-join above
them, because `Merge Append` merges two already-sorted streams and lets `LIMIT`
stop early, while `Append` over a `Hash Anti Join` has to build the whole hash
table before it can emit a row.

This measures the three anti-join shapes the library now generates, through
the real views rather than hand-written SQL:

    Member            overridable,     soft delete  -> full anti-join
    RosterMembership  NOT overridable, soft delete  -> tombstones only
    AuditEntry        NOT overridable, hard delete  -> no anti-join

Not part of the default suite (pytest only collects test_*.py). Run with:

    uv run pytest tests/probe_overridable.py -s -q

`OVERLAY_PROBE_ROWS` scales it; the default is deliberately small enough to
finish in a few seconds.
"""

import os
import time

import pytest
from django.db import connection


pytestmark = pytest.mark.django_db(transaction=True)

ROWS = int(os.environ.get("OVERLAY_PROBE_ROWS", 200_000))

SHAPES = [
    ("full anti-join", "member_view"),
    ("tombstones only", "rostermembership_view"),
    ("no anti-join", "auditentry_view"),
]


def sql(statement, *params):
    with connection.cursor() as cursor:
        cursor.execute(statement, params or None)


def plan(query):
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN (ANALYZE, BUFFERS) " + query)
        return [row[0] for row in cursor.fetchall()]


def nodes(lines):
    """The join/merge nodes in the plan, in order, deduplicated."""
    found = []
    for line in lines:
        stripped = line.strip().lstrip("-> ")
        for node in ("Merge Append", "Append", "Hash Anti Join", "Merge Anti Join", "Sort", "Incremental Sort"):
            if stripped.startswith(node):
                found.append(node)
    return " / ".join(dict.fromkeys(found))


def best_of(query, rounds=3):
    best = None
    for _ in range(rounds):
        started = time.perf_counter()
        with connection.cursor() as cursor:
            cursor.execute(query)
            cursor.fetchall()
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return best


def load():
    """Half the rows in the source, half organic in the base, no overrides —
    the same row count through every view so the three are comparable.

    Loaded with the base tables' triggers off, the same way probe_wide_scale
    does it: rostermembership's OverlayForeignKeys would otherwise check every
    row against a roster and member that this probe never creates. Nothing here
    measures write paths, so the constraint isn't what's under test."""
    half = ROWS // 2
    for table in ("member", "auditentry", "rostermembership"):
        sql(f"ALTER TABLE {table} DISABLE TRIGGER USER")  # noqa: S608 - fixed identifiers
    sql("INSERT INTO testapp_shared_membersource (name) SELECT 'n' || g FROM generate_series(1, %s) g", half)
    sql("INSERT INTO member (id, name, _overlay_deleted) SELECT g, 'o' || g, FALSE FROM generate_series(1, %s) g", half)

    sql("INSERT INTO testapp_shared_auditentrysource (note) SELECT 'n' || g FROM generate_series(1, %s) g", half)
    sql("INSERT INTO auditentry (id, note) SELECT g, 'o' || g FROM generate_series(1, %s) g", half)

    sql(
        "INSERT INTO testapp_shared_rostermembershipsource (roster_id, member_id, role) "
        "SELECT -g, -g, 'r' FROM generate_series(1, %s) g",
        half,
    )
    sql(
        "INSERT INTO rostermembership (id, roster_id, member_id, role, _overlay_deleted) "
        "SELECT g, g, g, 'r', FALSE FROM generate_series(1, %s) g",
        half,
    )

    for table in ("member", "auditentry", "rostermembership"):
        sql(f"ALTER TABLE {table} ENABLE TRIGGER USER")  # noqa: S608 - fixed identifiers

    for table in (
        "member",
        "auditentry",
        "rostermembership",
        "testapp_shared_membersource",
        "testapp_shared_auditentrysource",
        "testapp_shared_rostermembershipsource",
    ):
        sql(f"ANALYZE {table}")  # noqa: S608 - fixed identifiers


def test_anti_join_shapes():
    load()
    print(f"\n\n{ROWS:,} rows per view ({ROWS // 2:,} source + {ROWS // 2:,} organic), no overrides\n")

    print(f"{'shape':<22} {'ORDER BY id LIMIT 50':>22} {'count(*)':>12}   plan")
    print("-" * 104)
    for label, view in SHAPES:
        ordered = f"SELECT * FROM {view} ORDER BY id LIMIT 50"  # noqa: S608 - fixed identifiers
        counted = f"SELECT count(*) FROM {view}"  # noqa: S608 - fixed identifiers
        print(f"{label:<22} {best_of(ordered):>20.2f}ms {best_of(counted):>10.1f}ms   {nodes(plan(ordered))}")

    # Dropping the anti-join is only half of it. `Merge Append` can only merge
    # streams that already arrive sorted, and under NEGATIVE_ID the source
    # branch emits `-id`, which the source's primary key index cannot order —
    # so that side falls back to a Sort over a full Seq Scan and the merge is
    # bounded by it. An expression index on ((-id)) is what makes both sides
    # index scans. A UUID strategy needs nothing here: it emits the pk
    # unchanged, so the existing primary key index already serves.
    ordered = f"SELECT * FROM {SHAPES[2][1]} ORDER BY id LIMIT 50"  # noqa: S608 - fixed identifiers
    print(f"\nno anti-join, unordered source path : {best_of(ordered):>8.2f}ms   {nodes(plan(ordered))}")
    sql("CREATE INDEX IF NOT EXISTS probe_auditentrysource_neg_id ON testapp_shared_auditentrysource ((-id))")
    sql("ANALYZE testapp_shared_auditentrysource")
    print(f"no anti-join, ((-id)) on the source : {best_of(ordered):>8.2f}ms   {nodes(plan(ordered))}")

    print("\n--- full plan, no anti-join + ordered source")
    for line in plan(ordered)[:10]:
        print("   ", line[:140])

    print("\n--- full plan, full anti-join")
    for line in plan(f"SELECT * FROM {SHAPES[0][1]} ORDER BY id LIMIT 50")[:10]:  # noqa: S608
        print("   ", line[:140])
