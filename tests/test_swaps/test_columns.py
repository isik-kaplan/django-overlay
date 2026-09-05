"""Whether the view's own query still resolves against the candidate.

The select list and extra_where, and the one thing extra_where can do wrong
that costs more than a finding if it is not contained: a statement Postgres
rejects aborts the transaction every remaining check shares.
"""

from dataclasses import replace

import pytest
from django.db import connections, transaction

from django_overlay.swaps import (
    ERROR,
    verify_source_swap,
)
from tests.test_swaps.support import (
    _Rollback,
    analyze,
    assert_finding,
    assert_message,
    codes,
    green,
)
from tests.testapp.models import (
    FilteredSourceTest,
    UniqueTest,
)
from tests.testapp_shared.models import UniqueTestSource


pytestmark = pytest.mark.django_db


def test_a_widened_column_blocks_the_swap(db_cursor):
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("ALTER TABLE green_uniquetest ALTER COLUMN ssn TYPE varchar(40)")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_finding(report, "S002", ERROR, "character varying(20)", "character varying(40)")


def test_extra_where_that_does_not_resolve_blocks_the_swap(db_cursor):
    db_cursor.execute(
        "INSERT INTO testapp_shared_filteredsourcetestsource (id, first_name, active) VALUES (1, 'Jane', true)"
    )
    green(db_cursor, "testapp_shared_filteredsourcetestsource", "green_filtered")
    # A real candidate is the configured source with a different table on it,
    # so it carries the model's extra_where; green() alone does not.
    candidate = replace(FilteredSourceTest.get_source(), table="green_filtered")
    # `active` is not a field of the model, so the column check has no opinion
    # about it — extra_where is the only thing that names it.
    db_cursor.execute("ALTER TABLE green_filtered DROP COLUMN active")
    analyze(db_cursor, "green_filtered")

    report = verify_source_swap(FilteredSourceTest, candidate, identity_columns=["first_name"])

    # One line of what Postgres said, not all of it: the rest is a LINE/caret
    # excerpt of generated SQL nobody wrote and nobody can act on.
    assert_message(
        report,
        "S014",
        ERROR,
        'extra_where does not resolve against public.green_filtered: column "active" does not exist',
    )


def test_an_extra_where_that_still_resolves_says_nothing(db_cursor):
    db_cursor.execute(
        "INSERT INTO testapp_shared_filteredsourcetestsource (id, first_name, active) VALUES (1, 'Jane', true)"
    )
    green(db_cursor, "testapp_shared_filteredsourcetestsource", "green_filtered")
    candidate = replace(FilteredSourceTest.get_source(), table="green_filtered")

    report = verify_source_swap(FilteredSourceTest, candidate, identity_columns=["first_name"])

    assert "S014" not in codes(report)
    assert report.ok, str(report)


def test_a_source_key_column_does_not_have_to_be_called_id(db_cursor):
    """The view reads the source's key under the model's primary key name, so
    the model's pk column is the one column a source is not required to have.
    A candidate keyed on vendor_id and carrying no id at all is a valid source,
    and reporting it as missing a column would block every vendor whose key is
    named after their own system."""
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("ALTER TABLE green_uniquetest ADD COLUMN vendor_id integer")
    db_cursor.execute("UPDATE green_uniquetest SET vendor_id = id")
    db_cursor.execute("ALTER TABLE green_uniquetest DROP COLUMN id")
    analyze(db_cursor, "green_uniquetest")

    report = verify_source_swap(UniqueTest, replace(candidate, id_column="vendor_id"), identity_columns=["ssn"])

    assert "S002" not in codes(report), str(report)


def test_a_candidate_without_the_column_its_key_lives_in_is_reported(db_cursor):
    """The other half of the same rule: the source's own id column is the one
    column the view cannot do without, because it is what every id in the
    overlay is derived from."""
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("ALTER TABLE green_uniquetest DROP COLUMN id")
    analyze(db_cursor, "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_message(
        report,
        "S002",
        ERROR,
        "public.green_uniquetest has no column 'id', which the view selects.",
    )


@pytest.mark.django_db(databases=["default", "other"])
def test_a_failing_probe_hands_the_connection_back_usable():
    """The extra_where probe is the one check that expects to be rejected, and
    a rejected statement aborts the transaction it ran in. A preflight is a
    read-only thing a caller runs inside their own transaction, so leaving that
    transaction aborted would break the very thing it was asked to check --
    which is what the savepoint is for, and it has to be taken on the
    connection the probe actually runs on.

    Which means the second alias, and an explicit transaction on it: with one
    alias configured, a savepoint taken here and one taken on the default are
    the same call, and `other` is a MIRROR that the test case leaves in
    autocommit, where a failed statement poisons nothing.
    """
    with connections["other"].cursor() as cursor:
        try:
            with transaction.atomic(using="other"):
                cursor.execute(
                    "INSERT INTO testapp_shared_filteredsourcetestsource (id, first_name, active) "
                    "VALUES (1, 'Jane', true)"
                )
                green(cursor, "testapp_shared_filteredsourcetestsource", "green_filtered")
                candidate = replace(FilteredSourceTest.get_source(), table="green_filtered")
                cursor.execute("ALTER TABLE green_filtered DROP COLUMN active")
                analyze(cursor, "green_filtered")

                report = verify_source_swap(
                    FilteredSourceTest, candidate, identity_columns=["first_name"], using="other"
                )

                assert_finding(report, "S014", ERROR, "extra_where does not resolve")
                # The statement that proves it: on a poisoned transaction this
                # raises InFailedSqlTransaction instead of returning a row.
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)
                raise _Rollback
        except _Rollback:
            pass
