"""The management command over both halves of the procedure.

Two modes out of one command, and the tests are mostly about the seam between
them: which flags reach which function, what a bare invocation defaults to,
and that the command changes nothing on either path that the library would not
have changed on its own.
"""

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from django_overlay.management.commands import swap_source as swap_source_command
from django_overlay.management.commands.swap_source import Command as SwapSourceCommand
from django_overlay.swaps import (
    SwapReport,
    deployed_source,
)
from django_overlay.sync import resolve_schema
from tests.test_swaps.support import (
    analyze,
    green,
    point_at,
)
from tests.testapp.models import (
    UniqueTest,
)
from tests.testapp_shared.models import UniqueTestSource


pytestmark = pytest.mark.django_db


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
    # The identity columns reached the preflight: without them it reports S005
    # rather than checking, and the command would be quietly running the one
    # check that matters least.
    assert "S005" not in output
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == (
        "testapp_shared_uniquetestsource"
    )


def test_the_command_fails_when_the_preflight_does(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    UniqueTest.objects.create(ssn="222-22-2222")
    green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, '222-22-2222', '')")
    analyze(db_cursor, "green_uniquetest")

    with pytest.raises(CommandError) as raised:
        run(
            "testapp.UniqueTest",
            "--candidate-schema",
            "public",
            "--candidate-table",
            "green_uniquetest",
            "--identity-column",
            "ssn",
        )

    assert str(raised.value) == "Preflight failed. Nothing was changed."


def test_the_command_cuts_over(monkeypatch, db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    output = run("testapp.UniqueTest", "--identity-column", "ssn")

    assert output.rstrip().endswith("Swapped testapp.UniqueTest.")
    # The report, not a stand-in for it: printing `None` here would leave the
    # operator with a success line and no account of what was checked.
    assert "testapp.UniqueTest: public.testapp_shared_uniquetestsource -> public.green_uniquetest" in output
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == "green_uniquetest"


def test_the_command_can_dry_run(monkeypatch, db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    output = run("testapp.UniqueTest", "--identity-column", "ssn", "--dry-run")

    assert output.rstrip().endswith("Dry run — nothing was changed.")
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

    out = io.StringIO()
    with pytest.raises(CommandError) as raised:
        call_command("swap_source", "testapp.UniqueTest", "--identity-column", "ssn", stdout=out)

    assert str(raised.value) == "Swap refused. Nothing was changed."
    # And the findings that caused it, which is the only thing that says what
    # to fix. A refusal that prints nothing is a refusal nobody can act on.
    assert "S009" in out.getvalue()
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == (
        "testapp_shared_uniquetestsource"
    )


def test_the_command_refuses_half_a_candidate():
    """Either half alone is ambiguous -- a schema with no table names nothing,
    and a table with no schema would have to guess one."""
    for half in (("--candidate-schema", "public"), ("--candidate-table", "green_uniquetest")):
        with pytest.raises(CommandError) as raised:
            run("testapp.UniqueTest", *half)
        assert str(raised.value) == "--candidate-schema and --candidate-table go together."


def test_the_command_refuses_a_label_that_is_not_app_dot_model():
    with pytest.raises(CommandError) as raised:
        run("UniqueTest")

    assert str(raised.value) == "Expected app_label.ModelName, got 'UniqueTest'"


def test_the_command_refuses_a_model_that_does_not_exist():
    with pytest.raises(CommandError) as raised:
        run("testapp.NoSuchModel")

    assert "NoSuchModel" in str(raised.value)


def test_the_command_refuses_a_model_that_is_not_an_overlay():
    with pytest.raises(CommandError) as raised:
        run("testapp.PersonProfile")

    assert str(raised.value) == "testapp.PersonProfile is not an OverlayModel."


def parser():
    return SwapSourceCommand().create_parser("manage.py", "swap_source")


def test_the_command_line_is_what_the_documentation_says_it_is():
    """The parser *is* the interface: a lost `dest` or a default of the wrong
    type is a flag that still parses and no longer does what it says. None of
    it is reachable from a call_command() that passes the options by hand,
    which is how every other test here drives it."""
    given = parser().parse_args(
        [
            "testapp.UniqueTest",
            "--identity-column",
            "ssn",
            "--identity-column",
            "dob",
            "--allow",
            "S006",
            "--dry-run",
            "--min-row-ratio",
            "0.5",
            "--lock-timeout",
            "9s",
            "--database",
            "other",
        ]
    )

    assert given.model == "testapp.UniqueTest"
    # Repeatable, and collected under the name the preflight takes.
    assert given.identity_columns == ["ssn", "dob"]
    assert given.allow == ["S006"]
    assert given.dry_run is True
    # A float, not the string argparse would otherwise hand on: the ratio is
    # multiplied by a row count.
    assert given.min_row_ratio == 0.5
    assert given.lock_timeout == "9s"
    assert given.database == "other"


def test_the_defaults_are_the_ones_a_bare_invocation_gets():
    """Each of these is a decision. The empty lists have to be lists because
    both are appended to; the ratio and the timeout are the values documented
    as the defaults; and both candidate halves default to None because that is
    what the two modes are told apart by."""
    given = parser().parse_args(["testapp.UniqueTest"])

    assert given.identity_columns == []
    assert given.allow == []
    assert given.dry_run is False
    assert given.min_row_ratio == 0.9
    assert given.lock_timeout == "5s"
    assert given.database == "default"
    assert given.candidate_schema is None
    assert given.candidate_table is None


def test_the_help_says_what_each_flag_is_for():
    """`--help` is the whole of what a person gets before running something
    that rewrites a view and its triggers. The identity-column text especially:
    it is the only place that says leaving it out is not a smaller check but no
    check at all.

    Read off the parser's own actions and compared for equality, not searched
    for in the formatted output. A substring is still found inside a string
    that merely wraps it, and two flags sharing one sentence hide a change to
    either of them.
    """
    actions = {action.dest: action for action in parser()._actions}

    assert actions["model"].metavar == "app_label.ModelName"
    assert actions["identity_columns"].metavar == "FIELD"
    assert actions["allow"].metavar == "CODE"
    assert actions["identity_columns"].help == (
        "A field of the source's natural key. Repeat for a composite one. Without it "
        "nothing checks that the candidate means the same entity by an id as the current "
        "source does, which is the one failure that breaks everything and raises nothing."
    )
    assert actions["candidate_schema"].help == "Verify this table instead of cutting over."
    assert actions["candidate_table"].help == "Verify this table instead of cutting over."
    assert actions["dry_run"].help == (
        "Run the full preflight against the configured source and stop before the cutover."
    )
    assert actions["allow"].help == ("Downgrade one finding code (e.g. S006) from error to warning. Repeatable.")


def test_a_candidate_is_the_configured_source_with_a_different_table_on_it():
    """Both halves come from the command line, and everything else is carried
    over: how a source is read -- its id column, its extra_where -- is a
    model-level decision, and changing that at the same time as the table is a
    different operation with different consequences."""
    configured = UniqueTest.get_source()

    candidate = SwapSourceCommand()._candidate(
        UniqueTest, {"candidate_schema": "elsewhere", "candidate_table": "green_uniquetest"}
    )

    assert candidate.schema == "elsewhere"
    assert candidate.table == "green_uniquetest"
    assert candidate.id_column == configured.id_column
    assert candidate.extra_where == configured.extra_where


def test_every_option_reaches_the_preflight(monkeypatch):
    """handle() is wiring, and each option it forwards is one the preflight
    behaves differently for. A dropped keyword falls back to a default that is
    plausible enough to look like it worked -- `using` to the default database,
    the ratio to 0.9 -- so the only way to see it is to catch what arrives."""
    captured = {}

    def record(model, candidate, **kwargs):
        captured.update(kwargs)
        return SwapReport("testapp.UniqueTest", candidate, candidate, ())

    monkeypatch.setattr(swap_source_command, "verify_source_swap", record)

    run(
        "testapp.UniqueTest",
        "--candidate-schema",
        "public",
        "--candidate-table",
        "green_uniquetest",
        "--identity-column",
        "ssn",
        "--min-row-ratio",
        "0.5",
        "--database",
        "other",
    )

    assert captured == {"identity_columns": ["ssn"], "using": "other", "min_row_ratio": 0.5}


def test_every_option_reaches_the_cutover(monkeypatch):
    """The same for the other mode, which forwards three more -- and one of
    them, `allow`, is the difference between a swap that is refused and a swap
    that goes ahead."""
    captured = {}

    def record(model, **kwargs):
        captured.update(kwargs)
        return SwapReport("testapp.UniqueTest", UniqueTest.get_source(), UniqueTest.get_source(), ())

    monkeypatch.setattr(swap_source_command, "swap_source", record)

    run(
        "testapp.UniqueTest",
        "--identity-column",
        "ssn",
        "--allow",
        "S006",
        "--lock-timeout",
        "9s",
        "--dry-run",
        "--min-row-ratio",
        "0.5",
        "--database",
        "other",
    )

    assert captured == {
        "identity_columns": ["ssn"],
        "using": "other",
        "min_row_ratio": 0.5,
        "dry_run": True,
        "lock_timeout": "9s",
        "allow": ["S006"],
    }
