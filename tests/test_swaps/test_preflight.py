"""Which checks run, in what order, and what gates what.

The sequence rather than any one check: that a faithful copy raises nothing
that blocks, that a wrong shape stops the row-level probes before they run
against columns that were just found missing, and that the two failures which
stop everything -- a table that does not exist, an identity column the model
has no field for -- are reported rather than raised.
"""

import pytest

from django_overlay.sources import SourceTable
from django_overlay.swaps import (
    ERROR,
    ROW_CHECKS,
    WARNING,
    verify_source_swap,
)
from tests.test_swaps.support import (
    assert_finding,
    assert_header,
    assert_message,
    codes,
    error_codes,
    green,
)
from tests.testapp.models import (
    UniqueTest,
)
from tests.testapp_shared.models import UniqueTestSource


pytestmark = pytest.mark.django_db


def test_a_faithful_copy_raises_nothing_that_blocks(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert report.ok, str(report)
    assert_header(report, "testapp.UniqueTest", "public.testapp_shared_uniquetestsource", "public.green_uniquetest")
    assert "S003" not in codes(report)
    assert "S004" not in codes(report)


def test_a_missing_column_blocks_and_stops_the_row_level_checks(db_cursor):
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("ALTER TABLE green_uniquetest DROP COLUMN notes")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_finding(report, "S002", ERROR, "has no column 'notes'", "which the view selects")
    # S000 says the row-level probes were skipped, which is the point: they name
    # the columns that were just found missing.
    assert_header(report, "testapp.UniqueTest", "public.testapp_shared_uniquetestsource", "public.green_uniquetest")
    assert_message(
        report,
        "S000",
        WARNING,
        "Skipped the row-level checks: the candidate's shape has to be right before identity, "
        "references and uniqueness can be checked against it.",
    )
    assert "S009" not in codes(report)


def test_a_named_set_of_checks_runs_on_its_own(db_cursor):
    """`checks=` is how the cutover re-runs the row-level half under the lock,
    and it returns straight out of the preflight without touching the shape
    checks at all. Nothing else takes that branch, so nothing else would notice
    it reporting against the wrong pair of tables."""
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"], checks=ROW_CHECKS)

    assert_header(report, "testapp.UniqueTest", "public.testapp_shared_uniquetestsource", "public.green_uniquetest")
    assert report.ok, str(report)
    # The shape checks did not run: S015 and S013 come from those, and this
    # candidate would have drawn one.
    assert not codes(report)


def test_a_candidate_that_does_not_exist_stops_everything(db_cursor):
    report = verify_source_swap(UniqueTest, SourceTable(schema="public", table="not_a_table"))

    assert error_codes(report) == {"S001"}
    assert_header(report, "testapp.UniqueTest", "public.testapp_shared_uniquetestsource", "public.not_a_table")
    assert_message(report, "S001", ERROR, "public.not_a_table does not exist.")


def test_an_identity_column_the_model_has_no_field_for_is_reported(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["not_a_field"])

    assert_finding(report, "S016", ERROR, "'not_a_field'", "has no field for")
