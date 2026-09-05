"""Whether an id still means what it meant.

The failure the whole package exists for. Nothing raises for any of these, so
every test builds the drift by hand and asks the preflight to notice it -- a
candidate that hands the same id to a different entity, one that renumbers a
row it kept, and one that dropped a row a base row is standing on.
"""

import pytest

from django_overlay.swaps import (
    ERROR,
    WARNING,
    verify_source_swap,
)
from tests.test_swaps.support import (
    analyze,
    assert_message,
    green,
)
from tests.testapp.models import (
    UniqueTest,
)
from tests.testapp_shared.models import UniqueTestSource


pytestmark = pytest.mark.django_db


def test_an_unverified_identity_is_reported_rather_than_passed_over(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate)

    assert report.ok
    assert_message(
        report,
        "S005",
        WARNING,
        "No identity_columns given, so nothing verified that the candidate means the same entity "
        "by an id as the current source does. Renumbering is the one failure here that raises "
        "nothing, breaks nothing visibly, and silently repoints every override, tombstone and "
        "foreign key at a different row. Pass the source's natural key.",
    )


def test_an_id_that_now_means_a_different_row_blocks_the_swap(db_cursor):
    row = UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("UPDATE green_uniquetest SET ssn = %s WHERE id = %s", ["999-99-9999", row.id])

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert not report.ok
    assert_message(
        report,
        "S003",
        ERROR,
        "1 id(s) carry a different ['ssn'] in public.green_uniquetest than in "
        "public.testapp_shared_uniquetestsource. Every override, tombstone and foreign key "
        "holding one of those now points at a different entity, and nothing will raise.",
    )


def test_a_row_that_kept_its_identity_and_changed_id_blocks_the_swap(db_cursor):
    row = UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("UPDATE green_uniquetest SET id = %s WHERE id = %s", [row.id + 5000, row.id])

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert not report.ok
    assert_message(
        report,
        "S004",
        ERROR,
        "1 row(s) present in both tables changed id. References to them dangle and their "
        "overrides no longer shadow them.",
    )


def test_a_base_row_whose_source_row_the_candidate_lost_is_reported(db_cursor):
    kept = UniqueTestSource.objects.create(ssn="kept")
    dropped = UniqueTestSource.objects.create(ssn="dropped")
    # Touching it materialises a base row that shadows the source row.
    UniqueTest.objects.filter(pk=-dropped.id).update(notes="edited")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("DELETE FROM green_uniquetest WHERE id = %s", [dropped.id])
    analyze(db_cursor, "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_message(
        report,
        "S006",
        WARNING,
        "1 base row(s) are backed by a source row the candidate does not have. They keep their "
        "values and stay visible — materialisation copies the whole row — but they stop being "
        "vendor-backed: source_row() returns None and reset_to_source() has nothing to reset to.",
    )
    assert UniqueTest.objects.filter(pk=-kept.id).exists()
