"""How the candidate is indexed and partitioned.

Every finding here is a warning, so what these pin is that the warning is
accurate and legible: which indexes are missing, listed one per line, and
which of the three ways a partition declaration and a table can disagree.
"""

from dataclasses import replace

import pytest

from django_overlay.sources import SourceTable
from django_overlay.swaps import (
    WARNING,
    verify_source_swap,
)
from tests.test_swaps.support import (
    analyze,
    assert_finding,
    assert_message,
    codes,
    green,
)
from tests.testapp.models import (
    SoftDeleteUniqueTest,
    UniqueTest,
)
from tests.testapp_shared.models import UniqueTestSource


pytestmark = pytest.mark.django_db


def partitioned_green(cursor, name: str) -> SourceTable:
    cursor.execute(
        f'CREATE TABLE public."{name}" (LIKE public."testapp_shared_uniquetestsource") PARTITION BY RANGE (id)'
    )
    cursor.execute(
        f'CREATE TABLE public."{name}_p1" PARTITION OF public."{name}" FOR VALUES FROM (MINVALUE) TO (MAXVALUE)'
    )
    cursor.execute(f'INSERT INTO public."{name}" SELECT * FROM public."testapp_shared_uniquetestsource"')
    analyze(cursor, name)
    return SourceTable(schema="public", table=name)


def test_a_candidate_missing_an_index_the_current_source_has_is_a_warning(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("DROP INDEX green_uniquetest_ssn_idx")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_finding(report, "S010", WARNING, "is missing 1 index(es)", "btree (ssn)")
    assert report.ok, "a missing index is slow, not wrong"


def test_a_partitioned_candidate_with_no_declaration_is_reported(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = partitioned_green(db_cursor, "green_partitioned")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_message(
        report,
        "S012",
        WARNING,
        "public.green_partitioned is partitioned into 1 partitions and the source declares no "
        "partition_key, so every generated probe fans out across all of them.",
    )
    assert report.ok, "an unpruned probe is slow, not wrong"


def test_an_index_built_on_one_partition_is_reported(db_cursor):
    # The key is declared, so the only S012 left to say anything is the one
    # about the index. Two findings sharing a code is how a test ends up
    # asserting the presence of the wrong one.
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = replace(partitioned_green(db_cursor, "green_partitioned"), partition_key="id")
    db_cursor.execute("CREATE INDEX ON public.green_partitioned_p1 (ssn)")
    db_cursor.execute("CREATE INDEX ON public.green_partitioned_p1 (notes)")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_message(
        report,
        "S012",
        WARNING,
        "public.green_partitioned has indexes built on individual partitions rather than on the "
        "parent, so they cover some partitions and not others:"
        "\n      - btree (notes) (on 1 partitions)"
        "\n      - btree (ssn) (on 1 partitions)",
    )


def test_a_partition_key_declared_against_an_unpartitioned_candidate_is_reported(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = replace(
        green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest"),
        partition_key="id",
    )

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_message(
        report,
        "S012",
        WARNING,
        "partition_key='id' is declared, but public.green_uniquetest is not a partitioned table. "
        "Every generated probe carries a predicate that prunes nothing.",
    )


def test_a_declared_key_on_a_partitioned_candidate_is_not_reported(db_cursor):
    """The state the declaration exists to reach. Both partition warnings are
    about a *mismatch* between what is declared and what the table is, so a
    candidate where the two agree has to be silent -- otherwise the check
    reports the thing it is asking for."""
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = replace(partitioned_green(db_cursor, "green_partitioned"), partition_key="id")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert "S012" not in codes(report), str(report)


def test_indexes_the_candidate_lost_are_listed_one_per_line(db_cursor):
    """Two indexes dropped, so the list is a list: with one entry the separator
    between entries never appears, and neither does the difference between the
    parity check and the trigger-coverage check -- one reports what the current
    source has and the candidate does not, the other what this model's own
    triggers probe by."""
    db_cursor.execute(
        "INSERT INTO testapp_shared_softdeleteuniquetestsource (id, ssn, email, first_name, last_name) "
        "VALUES (1, 's-one', 'one@example.com', 'A', 'One')"
    )
    analyze(db_cursor, "testapp_shared_softdeleteuniquetestsource")
    candidate = green(db_cursor, "testapp_shared_softdeleteuniquetestsource", "green_softdeleteunique")
    db_cursor.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'green_softdeleteunique' "
        "AND (indexdef LIKE '%(ssn)%' OR indexdef LIKE '%(email)%')"
    )
    for (name,) in db_cursor.fetchall():
        db_cursor.execute(f'DROP INDEX public."{name}"')
    analyze(db_cursor, "green_softdeleteunique")

    report = verify_source_swap(SoftDeleteUniqueTest, candidate, identity_columns=["ssn"])

    assert_message(
        report,
        "S010",
        WARNING,
        "public.green_softdeleteunique is missing 2 index(es) that "
        "public.testapp_shared_softdeleteuniquetestsource has:"
        "\n      - btree (email)"
        "\n      - btree (ssn)",
    )
    assert_message(
        report,
        "S011",
        WARNING,
        "public.green_softdeleteunique has no index leading with:"
        "\n      - email: softdeleteuniquetest_email_uniq checks the source for a duplicate on every insert"
        "\n      - ssn: softdeleteuniquetest_ssn_unique checks the source for a duplicate on every insert",
    )
