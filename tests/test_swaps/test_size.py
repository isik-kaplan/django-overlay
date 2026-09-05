"""Roughly how much is in the candidate, and whether anyone analysed it.

Two of these are arithmetic rather than scenario -- a candidate exactly at the
ratio and one exactly the same size -- because a floor is only a floor if the
boundary is pinned, and a default above 1 would report every faithful copy as
suspiciously small.
"""

import pytest

from django_overlay.swaps import (
    ERROR,
    WARNING,
    swap_source,
    verify_source_swap,
)
from tests.test_swaps.support import (
    analyze,
    assert_finding,
    assert_message,
    codes,
    green,
    point_at,
)
from tests.testapp.models import (
    UniqueTest,
)
from tests.testapp_shared.models import UniqueTestSource


pytestmark = pytest.mark.django_db


def test_an_empty_candidate_blocks_the_swap(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest", copy_rows=False)

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_finding(report, "S013", ERROR, "is empty")


def test_an_unanalysed_candidate_is_reported(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest", analyzed=False)

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_message(
        report,
        "S015",
        WARNING,
        "public.green_uniquetest has never been analysed, so the planner has no statistics for it. "
        "The view is a UNION ALL, which is already the shape Postgres estimates worst — run "
        "ANALYZE before the cutover, not after.",
    )


def test_a_candidate_holding_far_fewer_rows_is_reported(db_cursor):
    for index in range(20):
        UniqueTestSource.objects.create(ssn=f"row-{index}")
    analyze(db_cursor, "testapp_shared_uniquetestsource")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("DELETE FROM green_uniquetest WHERE ssn <> 'row-0'")
    analyze(db_cursor, "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    # The percentage is pinned, not just its presence: it is a ratio, and a
    # ratio spelled with the wrong operator still renders something ending in a
    # per-cent sign.
    assert_message(
        report,
        "S013",
        WARNING,
        "public.green_uniquetest holds about 1 rows against 20 today (5%).",
    )
    assert report.ok, "a shrunken source is suspicious, not impossible"


def test_a_faithful_copy_is_not_reported_as_a_shrunken_one(monkeypatch, db_cursor):
    """min_row_ratio is a floor and the default has to be below 1: a candidate
    the same size as the source it replaces is the ordinary case, and a default
    above 1 would report every one of them as suspiciously small. Both tables
    are analysed, because the check reads reltuples and a source reporting zero
    rows skips the comparison entirely."""
    for index in range(10):
        UniqueTestSource.objects.create(ssn=f"row-{index}")
    analyze(db_cursor, "testapp_shared_uniquetestsource")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    report = swap_source(UniqueTest, identity_columns=["ssn"])

    assert "S013" not in codes(report), str(report)


def test_a_candidate_at_exactly_the_ratio_is_not_reported(db_cursor):
    """`min_row_ratio` is a floor, not a threshold to be at or above: 90% of
    the current size with a 0.9 ratio is the last acceptable value, not the
    first bad one. Also pins the default itself, and that the comparison is a
    multiplication -- dividing by 0.9 instead would flag everything under 111%,
    which is nearly every candidate."""
    for index in range(100):
        UniqueTestSource.objects.create(ssn=f"row-{index}")
    analyze(db_cursor, "testapp_shared_uniquetestsource")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("DELETE FROM green_uniquetest WHERE id IN (SELECT id FROM green_uniquetest LIMIT 10)")
    analyze(db_cursor, "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert "S013" not in codes(report), str(report)


def test_a_candidate_the_same_size_as_the_current_source_is_not_reported(db_cursor):
    """The ordinary case, and the one that says the size rule is a comparison
    rather than "there is a current source at all"."""
    for index in range(20):
        UniqueTestSource.objects.create(ssn=f"row-{index}")
    analyze(db_cursor, "testapp_shared_uniquetestsource")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert "S013" not in codes(report), str(report)
