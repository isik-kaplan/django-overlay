"""The M2M fence, through the ORM rather than hand-written SQL.

probe_m2m_variants.py measured hand-written forms. This checks that
`filter(phones__number=…)` actually emits the fenced form, that it is as fast
as the hand-written version, and -- the part that matters -- that the rows are
identical to the unfenced join, including their multiplicity.

Every comparison is ordered. An unordered LIMIT is free to return a different
arbitrary subset under a different plan, which says nothing either way.
"""
import time

import pytest
from django.test import override_settings

from benchmark.graph import load
from tests.testapp.models import BenchPerson


pytestmark = pytest.mark.django_db(transaction=True)

OFF = override_settings(DJANGO_OVERLAY_REWRITE_TRAVERSALS=False)


def timed(build, rounds=3):
    best, result = None, None
    for _ in range(rounds):
        started = time.perf_counter()
        result = list(build())
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return best, result


def test_orm_m2m_fence():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s\n")

    # Ordered, so the comparison is meaningful. Ordering by id also makes
    # duplicates adjacent, so a collapsed multiplicity shows up as a shorter list.
    def page(qs):
        return qs.order_by("id").values_list("id", flat=True)[:200]

    cases = {
        "selective  phones__number=": lambda: page(
            BenchPerson.objects.filter(phones__number="+447000000042")),
        "broad      phones__kind=": lambda: page(
            BenchPerson.objects.filter(phones__kind="mobile")),
        "two hops   addresses__city=": lambda: page(
            BenchPerson.objects.filter(addresses__city="city42")),
        "narrow     addresses__postcode=": lambda: page(
            BenchPerson.objects.filter(addresses__postcode="pc42")),
    }

    print(f"  {'query':<32} {'unfenced':>11} {'fenced':>10} {'gain':>9}   identical")
    print("  " + "-" * 82)
    failures = []
    for label, build in cases.items():
        with OFF:
            slow, plain_rows = timed(build)
        fast, fenced_rows = timed(build)
        identical = list(plain_rows) == list(fenced_rows)
        if not identical:
            failures.append((label, plain_rows, fenced_rows))
        print(f"  {label:<32} {slow:>9.1f}ms {fast:>8.1f}ms {slow / fast:>8.1f}x   "
              f"{'YES' if identical else 'NO -- BUG'}  ({len(plain_rows)} rows)")

    # Multiplicity, checked independently of any LIMIT: the total row count of
    # the whole traversal must be unchanged, duplicates included.
    print(f"\n  {'total row count (no limit)':<32} {'unfenced':>11} {'fenced':>10}   identical")
    print("  " + "-" * 82)
    for label, qs in (
        ("selective  phones__number=", BenchPerson.objects.filter(phones__number="+447000000042")),
        ("narrow     addresses__postcode=", BenchPerson.objects.filter(addresses__postcode="pc42")),
    ):
        with OFF:
            plain_total = qs.count()
        fenced_total = qs.count()
        same = plain_total == fenced_total
        if not same:
            failures.append((label + " count", plain_total, fenced_total))
        print(f"  {label:<32} {plain_total:>11,} {fenced_total:>10,}   "
              f"{'YES' if same else 'NO -- BUG'}")

    print("\n  emitted SQL (selective):")
    print("   ", str(BenchPerson.objects.filter(phones__number="+447000000042").query)[:420])

    for label, a, b in failures:
        print(f"\n  MISMATCH {label}:\n    unfenced={a}\n    fenced  ={b}")
    assert not failures
