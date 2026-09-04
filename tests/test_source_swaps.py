"""Blue-green swaps of a source table.

Every test here builds a real second table, points a model at it and asks
Postgres what happened, because the failures this feature exists to catch are
exactly the ones that produce no error: a swap leaves the view and its triggers
disagreeing, or leaves ids meaning something they did not mean before, and
nothing in Python can see either.
"""

import io
from dataclasses import replace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, connections, transaction

from django_overlay.exceptions import OverlaySwapRefused
from django_overlay.sources import SourceTable
from django_overlay.swaps import (
    ERROR,
    WARNING,
    Finding,
    SwapReport,
    _allow,
    deployed_source,
    swap_source,
    verify_source_swap,
)
from django_overlay.sync import resolve_schema, resync_view
from tests.testapp.models import (
    FilteredSourceTest,
    Person,
    PersonProfile,
    SoftDeleteTest,
    SoftDeleteTestNote,
    SoftDeleteUniqueTest,
    UniqueTest,
)
from tests.testapp_shared.models import PersonSource, SoftDeleteTestSource, UniqueTestSource


pytestmark = pytest.mark.django_db


def green(cursor, original: str, name: str, *, copy_rows: bool = True, analyzed: bool = True) -> SourceTable:
    """A candidate table shaped exactly like `original`. INCLUDING ALL so the
    index-parity checks have something real to compare, and ANALYZE so the row
    estimate is an estimate of something."""
    cursor.execute(f'DROP TABLE IF EXISTS public."{name}"')
    cursor.execute(f'CREATE TABLE public."{name}" (LIKE public."{original}" INCLUDING ALL)')
    if copy_rows:
        cursor.execute(f'INSERT INTO public."{name}" SELECT * FROM public."{original}"')
    if analyzed:
        analyze(cursor, name)
    return SourceTable(schema="public", table=name)


def analyze(cursor, name: str) -> None:
    """Every candidate is analysed before it is verified, because an unanalysed
    one is a finding in its own right (S015) and would mask the emptiness check
    behind it. Call it again after seeding a candidate directly."""
    cursor.execute(f'ANALYZE public."{name}"')


def point_at(monkeypatch, model, source: SourceTable) -> None:
    monkeypatch.setattr(model._overlay_meta, "get_source", staticmethod(lambda: source))


def codes(report) -> set:
    return {finding.code for finding in report.findings}


def error_codes(report) -> set:
    return {finding.code for finding in report.errors}


def finding(report, code):
    """The one finding carrying this code.

    Asserting a code is in the report leaves the two things the report actually
    promises unpinned: the level, which is what decides whether a swap is
    blocked or merely reported, and the sentence, which is the whole of what an
    operator gets. A check that silently downgraded an error to a warning would
    sail through a test that only looked for the code."""
    matches = [f for f in report.findings if f.code == code]
    assert len(matches) == 1, f"expected exactly one {code}, got {sorted(f.code for f in report.findings)}"
    return matches[0]


def assert_finding(report, code, level, *fragments):
    """The finding, its level, and the parts of its message that carry meaning
    rather than phrasing -- a count, a table name, the word that says which of
    two things went wrong."""
    found = finding(report, code)
    assert found.level == level, f"{code} came back {found.level}, expected {level}\n{report}"
    for fragment in fragments:
        assert fragment in found.message, f"{code} message is missing {fragment!r}:\n  {found.message}"
    return found


def assert_message(report, code, level, message):
    """The whole sentence, not a fragment of it.

    A finding *is* the output of a preflight -- an operator reads it and
    decides, on the strength of it alone, whether to cut a production source
    table over. A fragment assertion pins the clause it quotes and leaves every
    other clause free to say anything at all, including the opposite of what it
    says now, which is how a warning ends up describing the wrong failure.

    So one of these per finding code, alongside the fragment assertions that
    cover the scenarios. It makes rewording a finding a test change on purpose:
    here the wording is the feature, in a way it is not for an exception whose
    message nobody acts on.
    """
    found = finding(report, code)
    assert found.level == level, f"{code} came back {found.level}, expected {level}\n{report}"
    assert found.message == message, f"{code} says\n  {found.message}\nexpected\n  {message}"
    return found


# ------------------------------------------------------------ what is live


def test_deployed_source_reads_the_table_the_view_actually_uses():
    schema = resolve_schema(connection)
    assert deployed_source(connection, schema, UniqueTest) == SourceTable(
        schema="public", table="testapp_shared_uniquetestsource"
    )


def test_deployed_source_does_not_believe_get_source(monkeypatch, db_cursor):
    """The whole reason it introspects. Config naming green while the database
    still reads blue is not an error state, it is the middle of a swap."""
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    deployed = deployed_source(connection, resolve_schema(connection), UniqueTest)

    assert deployed.table == "testapp_shared_uniquetestsource"
    assert UniqueTest.get_source().table == "green_uniquetest"


# ----------------------------------------------------------------- verify


def test_a_faithful_copy_raises_nothing_that_blocks(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert report.ok, str(report)
    assert "S003" not in codes(report)
    assert "S004" not in codes(report)


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


def test_a_candidate_holding_a_value_a_base_row_already_holds_blocks_the_swap(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    UniqueTest.objects.create(ssn="222-22-2222")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, %s, '')", ["222-22-2222"])
    analyze(db_cursor, "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_message(
        report,
        "S009",
        ERROR,
        "uniquetest_ssn_unique: 1 row(s) in public.green_uniquetest hold a ['ssn'] that a base "
        "row already holds. The constraint would be violated the moment the view reads both, and "
        "no index or trigger raises for it.",
    )


def test_a_candidate_that_duplicates_a_constrained_value_within_itself_blocks_the_swap(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, '333-33-3333', '')")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9002, '333-33-3333', '')")
    analyze(db_cursor, "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_finding(report, "S008", ERROR, "uniquetest_ssn_unique", "more than once within")


def test_a_reference_the_candidate_would_strand_blocks_the_swap(db_cursor):
    """Two dangling references, one of which already dangled before anyone
    proposed a swap. The count in the finding is the one the swap is
    responsible for, because that is the number an operator is deciding on --
    the total is in the parenthesis beside it, not instead of it."""
    PersonSource.objects.create(first_name="Anchor")
    already = PersonSource.objects.create(first_name="Gone")
    stranded = PersonSource.objects.create(first_name="Jane")
    old_profile = PersonProfile.objects.create(person_id=-already.id, bio="")
    PersonProfile.objects.create(person_id=-stranded.id, bio="")
    # The vendor removed this one months ago; the swap neither caused it nor
    # can fix it.
    db_cursor.execute("DELETE FROM testapp_shared_personsource WHERE id = %s", [already.id])
    candidate = green(db_cursor, "testapp_shared_personsource", "green_person")
    db_cursor.execute("DELETE FROM green_person WHERE id = %s", [stranded.id])

    report = verify_source_swap(Person, candidate, identity_columns=["first_name"])

    assert_message(
        report,
        "S007",
        ERROR,
        "1 reference(s) in testapp_personprofile.person_id point at a row the candidate does "
        "not make visible (1 of 2 already dangle today).",
    )

    # Same reason as the warning case below: the reference to the row the
    # vendor deleted has a constraint-trigger event queued against a parent
    # that is legitimately gone, and it fires when this transaction ends.
    old_profile.delete()


def test_a_reference_that_already_dangles_is_not_blamed_on_the_swap(db_cursor):
    # Nothing has ever stopped the vendor deleting a referenced row, so a
    # reference can be dangling before the swap is even proposed. Reporting
    # that as something the candidate caused would block a cutover on a problem
    # the cutover neither creates nor can fix.
    PersonSource.objects.create(first_name="Anchor")
    stranded = PersonSource.objects.create(first_name="Jane")
    profile = PersonProfile.objects.create(person_id=-stranded.id, bio="")
    # The vendor deletes the row out from under the reference. Nothing here can
    # stop that -- we do not own that table and cannot put a trigger on it --
    # so a dangling reference can predate any swap by months.
    db_cursor.execute("DELETE FROM testapp_shared_personsource WHERE id = %s", [stranded.id])
    candidate = green(db_cursor, "testapp_shared_personsource", "green_person")

    report = verify_source_swap(Person, candidate, identity_columns=["first_name"])

    assert_message(
        report,
        "S007",
        WARNING,
        "1 reference(s) in testapp_personprofile.person_id already dangle today and still would. "
        "The swap does not cause this.",
    )

    # The reference's own constraint-trigger event is still queued against a
    # parent that is now legitimately gone, and would fire when this test's
    # transaction ends. Removing the reference is exactly the create-then-delete
    # case the trigger's first guard exists for.
    profile.delete()


def test_a_missing_column_blocks_and_stops_the_row_level_checks(db_cursor):
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("ALTER TABLE green_uniquetest DROP COLUMN notes")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_finding(report, "S002", ERROR, "has no column 'notes'", "which the view selects")
    # S000 says the row-level probes were skipped, which is the point: they name
    # the columns that were just found missing.
    assert_finding(report, "S000", WARNING, "Skipped the row-level checks")
    assert "S009" not in codes(report)


def test_a_widened_column_blocks_the_swap(db_cursor):
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("ALTER TABLE green_uniquetest ALTER COLUMN ssn TYPE varchar(40)")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_finding(report, "S002", ERROR, "character varying(20)", "character varying(40)")


def test_an_empty_candidate_blocks_the_swap(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest", copy_rows=False)

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_finding(report, "S013", ERROR, "is empty")


def test_a_candidate_missing_an_index_the_current_source_has_is_a_warning(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("DROP INDEX green_uniquetest_ssn_idx")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_finding(report, "S010", WARNING, "is missing 1 index(es)", "btree (ssn)")
    assert report.ok, "a missing index is slow, not wrong"


# ------------------------------------------------------------------- swap


def test_swapping_to_the_table_already_deployed_does_nothing():
    report = swap_source(UniqueTest, identity_columns=["ssn"])

    assert_finding(report, "S018", WARNING, "already reads", "Nothing to swap")


def test_a_refused_swap_leaves_the_view_where_it_was(monkeypatch, db_cursor):
    UniqueTest.objects.create(ssn="222-22-2222")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, '222-22-2222', '')")
    point_at(monkeypatch, UniqueTest, candidate)

    with pytest.raises(OverlaySwapRefused):
        swap_source(UniqueTest, identity_columns=["ssn"])

    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == (
        "testapp_shared_uniquetestsource"
    )


def test_a_dry_run_reports_and_changes_nothing(monkeypatch, db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    report = swap_source(UniqueTest, identity_columns=["ssn"], dry_run=True)

    assert report.ok
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == (
        "testapp_shared_uniquetestsource"
    )


def test_a_swap_repoints_the_view(monkeypatch, db_cursor):
    db_cursor.execute("INSERT INTO testapp_shared_uniquetestsource (id, ssn, notes) VALUES (7001, 'blue', '')")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("UPDATE green_uniquetest SET notes = 'from-green' WHERE id = 7001")
    point_at(monkeypatch, UniqueTest, candidate)

    swap_source(UniqueTest, identity_columns=["ssn"])

    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == "green_uniquetest"
    assert UniqueTest.objects.get(pk=-7001).notes == "from-green"


def test_a_swap_repoints_the_uniqueness_trigger(monkeypatch, db_cursor):
    """The regression this whole change exists for.

    The uniqueness trigger's body names the source table as literal PL/pgSQL
    text. Replacing the view without replacing the trigger leaves the view
    reading green and the constraint that is supposed to guard it asking blue —
    so a value green already holds is accepted, and the view then returns two
    rows for a column declared unique."""
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, 'only-in-green', '')")
    analyze(db_cursor, "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    swap_source(UniqueTest, identity_columns=["ssn"])

    with pytest.raises(IntegrityError):
        UniqueTest.objects.create(ssn="only-in-green")


def test_a_swap_repoints_an_inbound_foreign_key_trigger(monkeypatch, db_cursor):
    """The other half of the same bug, from the other side. The FK's
    insert-side trigger lives on the *referencing* table and names the target's
    source, so it is invisible to anything that only looks at the model being
    swapped."""
    PersonSource.objects.create(first_name="Jane")
    candidate = green(db_cursor, "testapp_shared_personsource", "green_person")
    db_cursor.execute("INSERT INTO green_person (id, first_name, age) VALUES (9001, 'Only In Green', NULL)")
    analyze(db_cursor, "green_person")
    point_at(monkeypatch, Person, candidate)

    swap_source(Person, identity_columns=["first_name"])

    with transaction.atomic():
        PersonProfile.objects.create(person_id=-9001, bio="")
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_resync_view_repoints_the_uniqueness_trigger(monkeypatch, db_cursor):
    """resync_overlay_views is what the docs point at for a source change, so
    it has to do the whole job on its own — not just the half swap_source()
    remembers to finish."""
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, 'only-in-green', '')")
    point_at(monkeypatch, UniqueTest, candidate)

    resync_view(UniqueTest)

    with pytest.raises(IntegrityError):
        UniqueTest.objects.create(ssn="only-in-green")


# ------------------------------------------------------- the rest of the report


def test_a_report_with_no_findings_says_so():
    source = SourceTable(schema="public", table="whatever")
    assert "no findings" in str(SwapReport("testapp.UniqueTest", source, source, ()))


def test_warnings_are_separable_from_errors(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate)

    assert {f.code for f in report.warnings} == codes(report)
    assert not report.errors


def test_a_candidate_that_does_not_exist_stops_everything(db_cursor):
    report = verify_source_swap(UniqueTest, SourceTable(schema="public", table="not_a_table"))

    assert error_codes(report) == {"S001"}
    assert_finding(report, "S001", ERROR, "public.not_a_table", "does not exist")


def test_an_identity_column_the_model_has_no_field_for_is_reported(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["not_a_field"])

    assert_finding(report, "S016", ERROR, "'not_a_field'", "has no field for")


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


def test_a_duplicate_the_current_source_already_has_is_not_blamed_on_the_swap(db_cursor):
    UniqueTestSource.objects.create(ssn="shared")
    UniqueTestSource.objects.create(ssn="shared")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert_message(
        report,
        "S008",
        WARNING,
        "uniquetest_ssn_unique: 1 ['ssn'] value(s) already appear more than once within the "
        "current source and still would. Nothing in this package has ever enforced uniqueness "
        "within the source itself.",
    )


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


# ------------------------------------------------------------------ partitions


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


# ------------------------------------------------------------ cutover control


def test_a_finding_can_be_allowed_through(monkeypatch, db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    UniqueTest.objects.create(ssn="222-22-2222")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, '222-22-2222', '')")
    analyze(db_cursor, "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    report = swap_source(UniqueTest, identity_columns=["ssn"], allow=["S009"])

    assert report.ok
    # Downgraded, not dropped: an accepted finding still has to be visible.
    assert "S009" in codes(report)
    assert "[allowed]" in str(report)
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == "green_uniquetest"


def test_the_source_to_check_against_can_be_given_explicitly(monkeypatch, db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    report = swap_source(
        UniqueTest,
        identity_columns=["ssn"],
        current=SourceTable(schema="public", table="testapp_shared_uniquetestsource"),
        dry_run=True,
    )

    assert report.ok
    assert report.current.table == "testapp_shared_uniquetestsource"


def test_a_view_that_is_not_deployed_cannot_be_swapped(monkeypatch, db_cursor):
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)
    db_cursor.execute("DROP VIEW public.uniquetest_view")

    assert deployed_source(connection, resolve_schema(connection), UniqueTest) is None
    with pytest.raises(OverlaySwapRefused) as refused:
        swap_source(UniqueTest, identity_columns=["ssn"])
    assert_finding(refused.value.report, "S017", ERROR, "Could not read a single source relation", "Pass current=")


def test_a_check_that_only_fails_under_the_lock_aborts_the_cutover(monkeypatch, db_cursor):
    """The preflight and the cutover are two moments, and a write can land
    between them. The re-run under the lock is what makes that survivable, so
    it has to actually stop the swap."""
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    # Patched on the module that *defines* it, not on the package that
    # re-exports it: swap_source() resolves the name as a global of its own
    # module, so patching the re-export would change nothing it reads.
    from django_overlay.swaps import cutover as swaps_module

    real = swaps_module.verify_source_swap
    calls = []

    def flaky(*args, **kwargs):
        report = real(*args, **kwargs)
        calls.append(report)
        if len(calls) == 1:
            return report
        return replace(report, findings=(Finding("S009", "error", "raced"),))

    monkeypatch.setattr(swaps_module, "verify_source_swap", flaky)

    with pytest.raises(OverlaySwapRefused):
        swap_source(UniqueTest, identity_columns=["ssn"])

    assert len(calls) == 2
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == (
        "testapp_shared_uniquetestsource"
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


# ------------------------------------------------------------- the command


def run(*args, **options):
    out = io.StringIO()
    call_command("swap_source", *args, stdout=out, **options)
    return out.getvalue()


def test_the_command_verifies_a_candidate_without_touching_anything(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    output = run(
        "testapp.UniqueTest",
        "--candidate-schema",
        "public",
        "--candidate-table",
        "green_uniquetest",
        "--identity-column",
        "ssn",
    )

    assert "green_uniquetest" in output
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == (
        "testapp_shared_uniquetestsource"
    )


def test_the_command_fails_when_the_preflight_does(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    UniqueTest.objects.create(ssn="222-22-2222")
    green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, '222-22-2222', '')")
    analyze(db_cursor, "green_uniquetest")

    with pytest.raises(CommandError):
        run(
            "testapp.UniqueTest",
            "--candidate-schema",
            "public",
            "--candidate-table",
            "green_uniquetest",
            "--identity-column",
            "ssn",
        )


def test_the_command_cuts_over(monkeypatch, db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    output = run("testapp.UniqueTest", "--identity-column", "ssn")

    assert "Swapped" in output
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == "green_uniquetest"


def test_the_command_can_dry_run(monkeypatch, db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    output = run("testapp.UniqueTest", "--identity-column", "ssn", "--dry-run")

    assert "nothing was changed" in output.lower()
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == (
        "testapp_shared_uniquetestsource"
    )


def test_the_command_reports_a_refusal_and_stops(monkeypatch, db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    UniqueTest.objects.create(ssn="222-22-2222")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, '222-22-2222', '')")
    analyze(db_cursor, "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    with pytest.raises(CommandError):
        run("testapp.UniqueTest", "--identity-column", "ssn")

    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == (
        "testapp_shared_uniquetestsource"
    )


def test_the_command_refuses_half_a_candidate():
    with pytest.raises(CommandError):
        run("testapp.UniqueTest", "--candidate-schema", "public")


def test_the_command_refuses_a_label_that_is_not_app_dot_model():
    with pytest.raises(CommandError):
        run("UniqueTest")


def test_the_command_refuses_a_model_that_does_not_exist():
    with pytest.raises(CommandError):
        run("testapp.NoSuchModel")


def test_the_command_refuses_a_model_that_is_not_an_overlay():
    with pytest.raises(CommandError):
        run("testapp.PersonProfile")


# --------------------------------------------------------- the whole procedure
#
# Everything above tests one check or one mechanism. What none of it shows is a
# swap happening to a tenant who has actually been using the model -- which is
# the only state a real cutover is ever performed from. So these two build that
# state first: a row the tenant has overridden, a row they have deleted, a row
# something else references, and a vendor refresh that changes values under all
# of it.


def a_populated_tenant(db_cursor):
    """The four states a base table can be in relative to its source, all at
    once: untouched, overridden, tombstoned, and referenced."""
    untouched = PersonSource.objects.create(first_name="Ada", age=36)
    overridden = PersonSource.objects.create(first_name="Grace", age=45)
    deleted = PersonSource.objects.create(first_name="Alan", age=41)

    # Touching a source-backed row copies it down; the base copy shadows the
    # source row from then on.
    Person.objects.filter(pk=-overridden.id).update(first_name="Grace H.")
    # Soft delete leaves a tombstone that hides the source row from the view.
    Person.objects.filter(pk=-deleted.id).delete()
    PersonProfile.objects.create(person_id=-untouched.id, bio="referenced")
    return untouched, overridden, deleted


def test_a_vendor_refresh_swaps_cleanly_over_a_tenant_who_has_been_using_it(monkeypatch, db_cursor):
    """The procedure, start to finish, on a tenant with something to lose.

    The vendor rebuilds the table: same ids meaning the same people, one
    person's details corrected, one person added. Every overlay semantic has to
    survive it -- an untouched row picks the refresh up, an overridden row does
    not, a tombstone still hides, a reference still resolves, and a row that
    only exists in the new table is simply there."""
    untouched, overridden, deleted = a_populated_tenant(db_cursor)
    candidate = green(db_cursor, "testapp_shared_personsource", "green_person")
    db_cursor.execute("UPDATE green_person SET age = 37 WHERE id = %s", [untouched.id])
    db_cursor.execute("UPDATE green_person SET age = 46 WHERE id = %s", [overridden.id])
    db_cursor.execute("INSERT INTO green_person (id, first_name, age) VALUES (9001, 'Katherine', 33)")
    analyze(db_cursor, "green_person")

    # Preflight while the old table is still the one being served.
    report = verify_source_swap(Person, candidate, identity_columns=["first_name"])
    assert report.ok, str(report)

    point_at(monkeypatch, Person, candidate)
    swap_source(Person, identity_columns=["first_name"])

    # An untouched row is a window onto the source, so it refreshes.
    assert Person.objects.get(pk=-untouched.id).age == 37
    # An overridden one is a copy, so it does not -- not the name the tenant
    # set, and not the age the vendor corrected underneath it either.
    assert Person.objects.get(pk=-overridden.id).first_name == "Grace H."
    assert Person.objects.get(pk=-overridden.id).age == 45
    # The tombstone still masks its row, which is only true if the swap kept
    # the id the tombstone was written against.
    assert not Person.objects.filter(pk=-deleted.id).exists()
    # And the reference still resolves, through the new table.
    assert PersonProfile.objects.get().person.first_name == "Ada"
    assert Person.objects.get(pk=-9001).first_name == "Katherine"


def test_a_swap_can_be_pointed_back_at_the_table_it_came_from(monkeypatch, db_cursor):
    """Rolling back is the same operation with the arguments the other way
    round, and the reason to keep the old table rather than drop it on success.

    Worth its own test because the second cutover is the one that runs against
    a database the first one already changed -- the deployed source it reads
    back is now the candidate of the previous swap."""
    untouched, overridden, _ = a_populated_tenant(db_cursor)
    blue = Person.get_source()
    candidate = green(db_cursor, "testapp_shared_personsource", "green_person")
    db_cursor.execute("UPDATE green_person SET age = 37 WHERE id = %s", [untouched.id])
    analyze(db_cursor, "green_person")

    point_at(monkeypatch, Person, candidate)
    swap_source(Person, identity_columns=["first_name"])
    assert Person.objects.get(pk=-untouched.id).age == 37

    point_at(monkeypatch, Person, blue)
    swap_source(Person, identity_columns=["first_name"])

    assert deployed_source(connection, resolve_schema(connection), Person).table == ("testapp_shared_personsource")
    assert Person.objects.get(pk=-untouched.id).age == 36
    assert Person.objects.get(pk=-overridden.id).first_name == "Grace H."
    assert PersonProfile.objects.get().person.first_name == "Ada"


# ------------------------------------------------------ the arithmetic itself
#
# Three checks here decide on a comparison rather than on the presence of a
# row, and a comparison is the one thing a test that only builds a broken
# candidate never exercises. Each of these builds a candidate that is
# *deliberately fine* and asserts silence, which is the only way to pin which
# side of a boundary the rule sits on.


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


def test_a_declared_key_on_a_partitioned_candidate_is_not_reported(db_cursor):
    """The state the declaration exists to reach. Both partition warnings are
    about a *mismatch* between what is declared and what the table is, so a
    candidate where the two agree has to be silent -- otherwise the check
    reports the thing it is asking for."""
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = replace(partitioned_green(db_cursor, "green_partitioned"), partition_key="id")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert "S012" not in codes(report), str(report)


def test_allowing_one_code_leaves_every_other_finding_alone():
    """`allow` names one code. Downgrading anything else would turn a list of
    accepted findings into a way of turning the preflight off, which is the one
    thing an escape hatch must not become."""
    source = SourceTable(schema="public", table="whatever")
    report = SwapReport(
        "testapp.UniqueTest",
        source,
        source,
        (
            Finding("S009", ERROR, "a collision"),
            Finding("S007", ERROR, "a dangling reference"),
            Finding("S006", WARNING, "an orphan"),
        ),
    )

    allowed = _allow(report, ["S009"])

    assert finding(allowed, "S009").level == WARNING
    assert "[allowed]" in finding(allowed, "S009").message
    # The other error is untouched, so the report still blocks.
    assert finding(allowed, "S007").level == ERROR
    assert not allowed.ok
    # And a warning whose code happens to be allowed is not re-marked: it was
    # never blocking, so there is nothing to accept.
    assert "[allowed]" not in finding(allowed, "S006").message


def test_the_recheck_under_the_lock_asks_the_same_question_as_the_preflight(monkeypatch, db_cursor):
    """The re-run is only worth taking a lock for if it checks the same thing.
    Dropping the identity columns, or the source it compares against, would
    leave a cutover that verifies less at the moment it matters most and still
    reports the preflight's clean result."""
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    from django_overlay.swaps import ROW_CHECKS
    from django_overlay.swaps import cutover as swaps_module

    real = swaps_module.verify_source_swap
    calls = []

    def record(model, cand, **kwargs):
        calls.append(kwargs)
        return real(model, cand, **kwargs)

    monkeypatch.setattr(swaps_module, "verify_source_swap", record)
    swap_source(UniqueTest, identity_columns=["ssn"])

    preflight, recheck = calls
    assert recheck["current"] == preflight["current"]
    assert recheck["identity_columns"] == preflight["identity_columns"] == ["ssn"]
    assert recheck["min_row_ratio"] == preflight["min_row_ratio"]
    # The shape half is the only thing the two differ on, and deliberately:
    # schema cannot change under the lock, rows can.
    assert preflight.get("checks") is None
    assert recheck["checks"] is ROW_CHECKS


# ------------------------------------- the narrowings that have to stay narrow


def test_a_base_row_overriding_its_own_source_row_is_not_a_collision(db_cursor):
    """A materialised row holds the same value as the source row it shadows, by
    construction -- materialisation copies the whole row. The collision probe
    has to recognise that pairing as one entity and not two, which under
    NEGATIVE_ID means matching a base pk of -5 against a source id of 5. Get the
    sign wrong and every materialised row in the tenant is reported as a
    collision with itself."""
    row = UniqueTestSource.objects.create(ssn="111-11-1111")
    UniqueTest.objects.filter(pk=-row.id).update(notes="edited")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert "S009" not in codes(report), str(report)


def test_a_value_a_tombstone_released_is_not_a_collision(db_cursor):
    """The state the source-side uniqueness trigger deliberately permits: a
    source row is tombstoned, so its value is free, and the tenant takes it for
    a row of their own. Nothing is wrong here and the swap must not say there
    is -- but only the soft-delete narrowing in the probe knows that, and
    without it every released value comes back as a blocking error."""
    released = UniqueTestSource.objects.create(ssn="111-11-1111")
    UniqueTest.objects.filter(pk=-released.id).delete()
    UniqueTest.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate, identity_columns=["ssn"])

    assert "S009" not in codes(report), str(report)


def test_a_reference_to_a_row_a_tombstone_hides_is_dangling(db_cursor):
    """A reference is dangling when the view cannot return the row, which is not
    the same as the row being gone: a tombstone leaves the base row in place and
    the source row behind it, and hides both. A probe that only asks whether a
    row exists finds one and reports nothing."""
    row = SoftDeleteTestSource.objects.create(first_name="Jane")
    note = SoftDeleteTestNote.objects.create(target_id=-row.id, text="")
    # Written straight to the base table, because the package's own triggers
    # refuse to create this state -- the delete side fires on UPDATE as well as
    # DELETE precisely so a soft delete cannot strand a reference. It is still
    # reachable, by every write that does not go through them: raw SQL, another
    # service on the same database, a reference that predates the constraint.
    db_cursor.execute(
        "INSERT INTO softdeletetest (id, first_name, _overlay_deleted) VALUES (%s, %s, TRUE)",
        [-row.id, "Jane"],
    )
    candidate = green(db_cursor, "testapp_shared_softdeletetestsource", "green_softdeletetest")

    report = verify_source_swap(SoftDeleteTest, candidate, identity_columns=["first_name"])

    assert_finding(report, "S007", WARNING, "testapp_softdeletetestnote.target_id", "already dangle today")

    note.delete()
    db_cursor.execute("DELETE FROM softdeletetest WHERE id = %s", [-row.id])


def test_a_constraint_after_a_clean_one_is_still_checked(db_cursor):
    """Three constraints on one model, and only the last of them is broken. A
    loop that stops at the first constraint it finds nothing wrong with reports
    nothing on any model whose first constraint happens to be fine, which is
    most of them."""
    db_cursor.execute(
        "INSERT INTO testapp_shared_softdeleteuniquetestsource (id, ssn, email, first_name, last_name) "
        "VALUES (1, 's-one', 'one@example.com', 'A', 'One'), (2, 's-two', 'two@example.com', 'B', 'Two')"
    )
    analyze(db_cursor, "testapp_shared_softdeleteuniquetestsource")
    candidate = green(db_cursor, "testapp_shared_softdeleteuniquetestsource", "green_softdeleteunique")
    db_cursor.execute("UPDATE green_softdeleteunique SET email = 'one@example.com' WHERE id = 2")
    analyze(db_cursor, "green_softdeleteunique")

    report = verify_source_swap(SoftDeleteUniqueTest, candidate, identity_columns=["ssn"])

    assert_finding(report, "S008", ERROR, "softdeleteuniquetest_email_uniq", "more than once within")


# ------------------------------------------------- what a source has to carry


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


# ------------------------------------------- the indexes a swap is a chance to fix


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


class _Rollback(Exception):
    """Unwinds the atomic block below, and nothing else."""


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
