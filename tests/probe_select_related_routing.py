"""select_related() routing, measured on the production-shaped graph."""
import time

import pytest
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from benchmark.graph import load
from tests.testapp.models import BenchPersonPhone


pytestmark = pytest.mark.django_db(transaction=True)
OFF = override_settings(DJANGO_OVERLAY_REDIRECT_SELECT_RELATED=False)


def timed(build, rounds=3):
    best, rows, queries = None, None, None
    for _ in range(rounds):
        started = time.perf_counter()
        with CaptureQueriesContext(connection) as captured:
            rows = list(build())
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
        queries = len(captured)
    return best, rows, queries


def test_routing():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s\n")

    cases = {
        "select_related('person')": lambda: BenchPersonPhone.objects.select_related("person").order_by("id")[:100],
        "select_related('person','phone')":
            lambda: BenchPersonPhone.objects.select_related("person", "phone").order_by("id")[:100],
        "select_related() bare": lambda: BenchPersonPhone.objects.select_related().order_by("id")[:100],
    }

    print(f"  {'query':<36} {'joined':>11} {'routed':>10} {'gain':>9}  {'queries':>9}  same rows")
    print("  " + "-" * 88)
    for label, build in cases.items():
        with OFF:
            slow, joined_rows, joined_q = timed(build)
        fast, routed_rows, routed_q = timed(build)
        same = [r.pk for r in joined_rows] == [r.pk for r in routed_rows]
        print(f"  {label:<36} {slow:>9.1f}ms {fast:>8.1f}ms {slow / fast:>8.1f}x  "
              f"{joined_q}->{routed_q:<6}  {'YES' if same else 'NO -- BUG'}")
        assert same

        # and the related objects must be populated, not lazy-loaded
        with CaptureQueriesContext(connection) as captured:
            for row in routed_rows:
                assert row.person.city is not None
        assert len(captured) == 0, "prefetch did not populate the cache"
