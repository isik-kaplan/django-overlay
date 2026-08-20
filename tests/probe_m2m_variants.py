"""M2M traversal: does a multiplicity-preserving rewrite get the win?

`filter(phones__kind='mobile')` must return a person once per matching phone.
A semi-join returns them once, full stop, so the fast form measured in
the `shapes` benchmark suite is NOT equivalent to what Django emits. This measures the
forms that preserve row multiplicity.
"""
import time

import pytest

from benchmark.graph import PLAIN, best_of, load, plan, rows, shape_of


pytestmark = pytest.mark.django_db(transaction=True)

SELECTIVE = "p.number = '+447000000042'"
BROAD = "p.kind = 'mobile'"


def report(label, statement, expect_rows=True):
    ms, timed_out = best_of(statement)
    count = len(rows(statement)) if not timed_out else -1
    print(f"  {label:<52} {ms:>9.1f}ms  {count:>8} rows  {shape_of(plan(statement)):>15}")
    return ms


def test_variants():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s\n")

    for name, predicate in (("SELECTIVE (one number)", SELECTIVE), ("BROAD (kind=mobile)", BROAD)):
        print("=" * 100)
        print(name)
        print("=" * 100)

        report("1. Django's join (baseline, multiplicity-preserving)",
               f"SELECT person.id FROM bench_person_view person "
               f"JOIN bench_person_phone_view l ON l.person_id = person.id "
               f"JOIN bench_phone_view p ON p.id = l.phone_id WHERE {predicate} LIMIT 50")

        report("2. semi-join ARRAY (DEDUPLICATES -- not equivalent)",
               f"SELECT person.id FROM bench_person_view person WHERE person.id = ANY (ARRAY("
               f"SELECT l.person_id FROM bench_person_phone_view l WHERE l.phone_id = ANY (ARRAY("
               f"SELECT p.id FROM bench_phone_view p WHERE {predicate})))) LIMIT 50")

        report("3. join person<->link, fence only the phone side",
               f"SELECT person.id FROM bench_person_view person "
               f"JOIN bench_person_phone_view l ON l.person_id = person.id "
               f"WHERE l.phone_id = ANY (ARRAY(SELECT p.id FROM bench_phone_view p "
               f"WHERE {predicate})) LIMIT 50")

        report("4. as 3, plus a redundant fence on person",
               f"SELECT person.id FROM bench_person_view person "
               f"JOIN bench_person_phone_view l ON l.person_id = person.id "
               f"WHERE l.phone_id = ANY (ARRAY(SELECT p.id FROM bench_phone_view p "
               f"WHERE {predicate})) "
               f"AND person.id = ANY (ARRAY(SELECT l2.person_id FROM bench_person_phone_view l2 "
               f"WHERE l2.phone_id = ANY (ARRAY(SELECT p.id FROM bench_phone_view p "
               f"WHERE {predicate})))) LIMIT 50")

        report("5. plain table baseline",
               f"SELECT person.id FROM {PLAIN['person']} person "
               f"JOIN {PLAIN['person_phone']} l ON l.person_id = person.id "
               f"JOIN {PLAIN['phone']} p ON p.id = l.phone_id WHERE {predicate} LIMIT 50")
        print()
