"""The parts of the benchmark tooling that encode a judgement.

Most of `benchmark/` is measurement, and measurement is checked by running it.
These few pieces are different: they decide what gets *shown*, and a mistake in
any of them produces a plausible number rather than an obvious failure.

  * the noise floor -- print a 5% move and somebody goes hunting for a
    regression that is measurement error;
  * the environment guard -- compare across a Postgres version or a work_mem
    and the delta is meaningless while looking authoritative;
  * the capped-cell rules -- a capped cell only says "at least this slow", so
    any ratio or percentage built from one is invented;
  * the runtime estimate -- what the budget guard skips suites on.

None of this needs a database.
"""

import json
import pathlib
import time

import pytest
from django.db import OperationalError

from benchmark import environment, estimates, harness, results, switches
from benchmark.cli import parse_duration


# ------------------------------------------------------------- durations


@pytest.mark.parametrize(("text", "seconds"), [
    ("1h", 3600), ("45m", 2700), ("600s", 600), ("600", 600), ("1.5h", 5400),
])
def test_durations_are_read_the_way_they_are_written(text, seconds):
    assert parse_duration(text) == seconds


def test_an_unreadable_duration_is_rejected():
    from click import BadParameter
    with pytest.raises(BadParameter):
        parse_duration("soon")


# ----------------------------------------------------------------- cells


def test_a_capped_cell_renders_as_a_bound_not_a_number():
    assert harness.Cell(30_000.0, capped=True).render(30_000) == ">30s"
    assert harness.Cell(412.7).render(30_000) == "413ms"


def test_no_gain_is_claimed_when_either_side_capped():
    """A capped cell is a lower bound, so a ratio from one understates it."""
    assert harness.gain(harness.Cell(1000, capped=True), harness.Cell(100)) == ""
    assert harness.gain(harness.Cell(1000), harness.Cell(100, capped=True)) == ""
    assert harness.gain(harness.Cell(1000), harness.Cell(100)) == "x10.0"


# ---------------------------------------------------------------- deltas


def cell(ms, capped=False):
    return {"ms": ms, "capped": capped}


def test_a_move_inside_the_noise_floor_is_not_shown():
    assert results._delta_text(cell(100), harness.Cell(110)) == ""
    assert results._delta_text(cell(100), harness.Cell(90)) == ""


def test_a_move_past_the_noise_floor_is_shown_with_its_direction():
    assert results._delta_text(cell(100), harness.Cell(150)) == "(+50%)"
    assert results._delta_text(cell(100), harness.Cell(50)) == "(-50%)"


def test_crossing_the_cap_is_described_rather_than_quantified():
    assert results._delta_text(cell(30_000, capped=True), harness.Cell(400)) == "(was capped)"
    assert results._delta_text(cell(400), harness.Cell(30_000, capped=True)) == "(now capped)"
    assert results._delta_text(
        cell(30_000, capped=True), harness.Cell(30_000, capped=True)
    ) == ""


def test_deltas_are_only_offered_for_rows_the_baseline_also_has():
    """A row added since the baseline must not read as 'unchanged'."""
    section = harness.Section("S", ("overlay",))
    section.add("existing", {"overlay": harness.Cell(200)})
    section.add("brand new", {"overlay": harness.Cell(200)})
    baseline = {
        "suites": [{
            "name": "demo",
            "sections": [{
                "title": "S",
                "rows": [{"label": "existing", "cells": {"overlay": cell(100)}}],
            }],
        }],
    }
    deltas = results.deltas_for(baseline, "demo", section)
    assert deltas == {("existing", "overlay"): "(+100%)"}


# ----------------------------------------------------------- environment


def an_environment(**overrides):
    base = {
        "postgres_major": "17", "work_mem": "4MB", "shared_buffers": "128MB",
        "max_parallel_workers_per_gather": "2", "scale": 1.0, "share": 0.4,
        "cap_ms": 30_000, "cores": 14,
    }
    return base | overrides


@pytest.mark.parametrize("difference", [
    {"postgres_major": "16"},
    {"work_mem": "16MB"},
    {"shared_buffers": "1GB"},
    {"scale": 0.3},
    {"cap_ms": 10_000},
    {"cores": 2},
])
def test_runs_from_different_machines_are_not_comparable(difference):
    assert not environment.comparable(an_environment(), an_environment(**difference))


def test_an_identical_environment_is_comparable():
    assert environment.comparable(an_environment(), an_environment())


def test_the_reason_a_comparison_was_refused_is_reported():
    baseline = {"label": "before", "environment": an_environment(), "saved_at": "now"}
    note = results.comparison_note(baseline, an_environment(work_mem="16MB"))
    assert "no delta column" in note
    assert "work_mem 4MB -> 16MB" in note


def test_a_usable_baseline_says_so():
    baseline = {"label": "before", "environment": an_environment(), "saved_at": "now"}
    assert results.comparison_note(baseline, an_environment()).startswith("delta column")


def test_no_baseline_means_no_note():
    assert results.comparison_note(None, an_environment()) is None


# ------------------------------------------------------------- estimates


def test_estimates_interpolate_between_measured_points():
    at_low = estimates.for_suite("ban", 0.3)
    at_high = estimates.for_suite("ban", 1.0)
    assert at_low < estimates.for_suite("ban", 0.65) < at_high


def test_the_curves_differ_enough_per_suite_to_need_measuring_separately():
    """This is why MEASURED is a table rather than one scaling rule.

    From 0.05 to 1.0 the ban suite grows about 30x and aggregation about 4x,
    for the same twentyfold increase in rows -- because the shapes the ban
    suite exists to measure are the ones that start running past the cap at
    the larger size, and aggregation's already read the whole table at both.
    Any single rule fitted to one of them is badly wrong about the other.
    """
    def growth(name):
        return estimates.for_suite(name, 1.0) / estimates.for_suite(name, 0.05)

    assert growth("ban") > 20
    assert growth("aggregation") < 6
    assert growth("ban") > 4 * growth("aggregation")


def test_estimates_extrapolate_above_the_measured_range():
    assert estimates.for_suite("ban", 5.0) > estimates.for_suite("ban", 3.0)


def test_more_passes_cost_proportionally_more():
    assert estimates.for_suite("ban", 1.0, passes=2) == 2 * estimates.for_suite("ban", 1.0)


def test_an_unknown_suite_has_no_estimate():
    assert estimates.for_suite("not-a-suite", 1.0) is None


def test_a_cold_build_is_counted_into_the_run():
    warm, _, build = estimates.for_run(["ban"], 1.0, cold_build=False)
    cold, _, cold_build = estimates.for_run(["ban"], 1.0, cold_build=True)
    assert build == 0
    assert cold_build > 0
    assert cold == warm + cold_build


# ---------------------------------------------------------------- budget


def test_the_budget_refuses_a_suite_it_cannot_fit():
    budget = harness.Budget(max_seconds=60)
    assert budget.can_afford(10)
    assert not budget.can_afford(600)


def test_durations_read_back_the_way_a_person_would_say_them():
    assert harness.humanise(45) == "45s"
    assert harness.humanise(125) == "2m05s"
    assert harness.humanise(3900) == "1h05m"


# ------------------------------------------------ the third cell state


def test_a_skipped_cell_is_not_reported_as_a_slow_one():
    """'we never ran this' and 'this ran past the cap' are different claims."""
    assert harness.Cell(0.0, note="skipped").render(10_000) == "skipped"
    assert harness.Cell(0.0, capped=True).render(10_000) == ">10s"
    assert not harness.Cell(0.0, note="skipped").measured
    assert not harness.Cell(1.0, capped=True).measured
    assert harness.Cell(1.0).measured


def test_no_gain_is_claimed_against_a_skipped_cell():
    assert harness.gain(harness.Cell(1000), harness.Cell(0.0, note="skipped")) == ""
    assert harness.gain(harness.Cell(0.0, note="skipped"), harness.Cell(100)) == ""


def test_no_delta_is_offered_against_a_skipped_cell():
    assert results._delta_text(cell(100), harness.Cell(0.0, note="skipped")) == ""
    assert results._delta_text(
        {"ms": 0.0, "capped": False, "note": "skipped"}, harness.Cell(100)
    ) == ""


def test_a_context_past_its_deadline_skips_instead_of_measuring():
    """The statement cap bounds Postgres, not Django marshalling 300k keys."""
    import time

    from benchmark.suites import Context

    ran = []
    ctx = Context(scale=1.0, passes=1, cap_ms=10_000,
                  deadline=time.monotonic() - 1)
    measured, value = ctx.measure(lambda: ran.append(1))
    assert ran == [], "the build should not have been called at all"
    assert measured.note == "skipped"
    assert value is None


def test_a_context_inside_its_deadline_measures_normally():
    import time

    from benchmark.suites import Context

    ctx = Context(scale=1.0, passes=1, cap_ms=10_000,
                  deadline=time.monotonic() + 60)
    measured, value = ctx.measure(lambda: 42, rounds=1)
    assert measured.measured
    assert value == 42


def test_a_context_with_no_deadline_never_skips():
    from benchmark.suites import Context

    ctx = Context(scale=1.0, passes=1, cap_ms=10_000)
    assert not ctx.out_of_time()


# ----------------------------------------- abandoning a measurement in flight
#
# The deadline check above happens before a measurement starts, which cannot
# stop one already running. In CI the `staged` suite -- estimated at twenty-one
# seconds -- spent thirty-seven minutes inside a single execution before the
# job timeout killed the whole run, with twenty-two minutes of budget still
# unspent. These cover the ceiling that now bounds each execution.


def test_a_measurement_that_overruns_its_ceiling_is_abandoned():
    def never_returns():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            pass

    started = time.monotonic()
    measured, value = harness.measure(
        never_returns, 10_000, rounds=1, abandon_after_s=0.2
    )
    assert measured.note == "gave up"
    assert value is None
    assert time.monotonic() - started < 5, "the ceiling did not interrupt anything"


def test_an_abandoned_cell_is_not_a_capped_one():
    """'we stopped waiting' is not 'Postgres gave up', and not a duration."""
    gave_up = harness.Cell(0.0, note="gave up")
    assert gave_up.render(10_000) == "gave up"
    assert not gave_up.measured
    assert harness.gain(harness.Cell(1000), gave_up) == ""


def test_the_ceiling_is_lifted_once_the_measurement_finishes():
    """A timer left armed would fire in the middle of the next measurement."""
    harness.measure(lambda: 1, 10_000, rounds=1, abandon_after_s=0.3)
    slow_but_fine = harness.measure(
        lambda: time.sleep(0.5), 10_000, rounds=1, abandon_after_s=5
    )
    assert slow_but_fine[0].measured


def test_no_ceiling_means_no_interruption():
    measured, value = harness.measure(lambda: 7, 10_000, rounds=1, abandon_after_s=None)
    assert measured.measured
    assert value == 7


def test_the_ceiling_never_outlasts_the_run_budget():
    """One row is not allowed to eat the time the remaining suites need."""
    from benchmark.suites import Context

    roomy = Context(scale=1.0, passes=1, cap_ms=10_000, deadline=time.monotonic() + 3600)
    assert roomy.measurement_ceiling() == 60.0

    nearly_done = Context(scale=1.0, passes=1, cap_ms=10_000,
                          deadline=time.monotonic() + 5)
    assert nearly_done.measurement_ceiling() <= 5

    unbounded = Context(scale=1.0, passes=1, cap_ms=10_000)
    assert unbounded.measurement_ceiling() == 60.0


def test_a_small_cap_still_gets_a_usable_ceiling():
    """Six times a one-second cap is not long enough to be worth enforcing."""
    from benchmark.suites import Context

    ctx = Context(scale=1.0, passes=1, cap_ms=1_000)
    assert ctx.measurement_ceiling() == 30.0


# --------------------------------------- a cap and a broken connection differ
#
# Both arrive as OperationalError, and for one CI run they shared a cell. A
# connection was left with an unconsumed result inside `staged`; every statement
# after it failed instantly with "another command is already in progress"; and
# five rows printed `>10s did not finish` for queries that were never sent.


class _DriverError(Exception):
    """Stands in for psycopg's error: an exception carrying a SQLSTATE."""

    def __init__(self, sqlstate):
        super().__init__(sqlstate or "no sqlstate")
        self.sqlstate = sqlstate


def _operational(sqlstate, message="boom"):
    error = OperationalError(message)
    error.__cause__ = _DriverError(sqlstate)
    return error


def _raise(error):
    def build():
        raise error

    return build


def test_a_statement_timeout_is_reported_as_capped():
    measured, value = harness.measure(
        _raise(_operational("57014", "canceling statement due to statement timeout")),
        10_000,
        rounds=1,
    )
    assert measured.capped
    assert measured.render(10_000) == ">10s"
    assert value is None


def test_a_lock_timeout_is_reported_as_capped():
    measured, _ = harness.measure(_raise(_operational("55P03")), 10_000, rounds=1)
    assert measured.capped


def test_a_broken_connection_is_not_reported_as_capped(capsys):
    measured, value = harness.measure(
        _raise(_operational(None, "sending query failed: another command is already in progress")),
        10_000,
        rounds=1,
    )
    assert not measured.capped, "a query that was never sent is not a slow query"
    assert measured.note == "conn lost"
    assert not measured.measured
    assert value is None
    assert "LOST CONNECTION" in capsys.readouterr().err


def test_an_abandoned_measurement_says_what_state_it_left_behind(capsys):
    """The mid-run connection wedge has never reproduced locally; this is the evidence."""
    harness.measure(lambda: time.sleep(5), 10_000, rounds=1, abandon_after_s=0.2)
    reported = capsys.readouterr().err
    assert "ABANDONED" in reported
    assert "connection was" in reported and "now" in reported


def test_a_lost_connection_says_what_state_it_was_in(capsys):
    harness.measure(_raise(_operational(None, "gone")), 10_000, rounds=1)
    assert "[was " in capsys.readouterr().err


def test_the_reason_a_connection_was_lost_reaches_the_log(capsys):
    """The cell has room for two words; the cause has to go somewhere."""
    harness.measure(_raise(_operational(None, "server closed the connection")), 10_000, rounds=1)
    assert "server closed the connection" in capsys.readouterr().err


def test_an_error_with_no_driver_cause_is_treated_as_a_lost_connection():
    """Unrecognised means unknown, and unknown must not be filed as a timing."""
    measured, _ = harness.measure(_raise(OperationalError("no cause attached")), 10_000, rounds=1)
    assert measured.note == "conn lost"


def test_a_lost_connection_yields_no_gain_and_no_ratio():
    lost = harness.Cell(0.0, note="conn lost")
    assert lost.render(10_000) == "conn lost"
    assert harness.gain(harness.Cell(1000), lost) == ""


# ------------------------------------------------------ the suite registry
#
# The suites used to have a duplicate each under tests/probe_*.py, which meant
# every measurement existed twice and a change to one silently made the other a
# lie. The duplicates are gone; these keep the single copy honest, in the
# ordinary test run rather than only in the benchmark job.


def test_every_registered_suite_imports_and_conforms():
    from benchmark.suites import SUITE_NAMES, load_suite

    for name in SUITE_NAMES:
        suite = load_suite(name)
        assert suite.NAME == name, f"{name}: NAME must match the module it lives in"
        assert suite.TITLE and isinstance(suite.TITLE, str)
        assert callable(suite.run)


def test_every_suite_yields_its_sections_rather_than_returning_them():
    """Streaming is what lets a forty-minute run print as it goes."""
    import inspect

    from benchmark.suites import SUITE_NAMES, load_suite

    for name in SUITE_NAMES:
        assert inspect.isgeneratorfunction(load_suite(name).run), f"{name}.run must be a generator"


def test_no_suite_module_is_left_unregistered():
    """A file dropped into suites/ that nobody added to SUITE_NAMES would never
    run, and would look like it does."""
    import pathlib

    from benchmark.suites import SUITE_NAMES

    directory = pathlib.Path(__file__).resolve().parent.parent / "benchmark" / "suites"
    on_disk = {path.stem for path in directory.glob("*.py")} - {"__init__"}
    assert on_disk == set(SUITE_NAMES)


def test_every_suite_has_a_runtime_estimate():
    """Without one the budget guard cannot decide whether to start it."""
    from benchmark.suites import SUITE_NAMES

    for name in SUITE_NAMES:
        assert estimates.for_suite(name, 1.0) is not None, f"{name} has no measured points"


def test_the_smoke_selection_is_a_real_subset():
    from benchmark.suites import SMOKE, SUITE_NAMES

    assert set(SMOKE) < set(SUITE_NAMES)


# ------------------------------------------------- the optimisation switches
#
# The four library optimisations are what the benchmark exists to price, and
# until the CLI could turn them off the only question it could answer was
# "overlay against a plain table". Comparing against master is not the same
# question: none of these mechanisms exists there, and neither does this
# harness. So each one is a flag, and the flags have to reach the library --
# an arm that silently ran with everything on would report the default arm
# twice and call the difference noise.


def _tri_state_flags():
    """The CLI's paired flags that have no default of their own.

    That is exactly what a switch is and nothing else in the command is one:
    `--keep-up/--down` defaults to True, `--no-optimisations` to False. So the
    set is checkable in both directions without a name heuristic -- a switch
    with no flag fails, and a flag the switch table has never heard of fails
    too, which is the one that would move nothing.
    """
    from benchmark.cli import benchmark as command

    return {
        parameter.name: parameter for parameter in command.params
        if getattr(parameter, "is_bool_flag", False) and parameter.default is None
    }


def test_the_cli_exposes_exactly_the_switch_table():
    assert set(_tri_state_flags()) == {switches.option_name(s) for s in switches.SWITCHES}


def test_each_flag_is_spelled_the_way_the_table_spells_it():
    """The table's spelling is the one settings.py and the saved environment
    use. A flag that disagrees with it is a flag that moves nothing."""
    flags = _tri_state_flags()
    for switch in switches.SWITCHES:
        parameter = flags[switches.option_name(switch)]
        assert f"--{switch.flag}" in parameter.opts
        assert f"--no-{switch.flag}" in parameter.secondary_opts
        assert parameter.help == switch.help, (
            f"{switch.flag} explains itself differently in the CLI and the table"
        )


def test_an_unset_switch_reads_as_on():
    """Matching the library's own getattr(settings, name, True)."""
    assert switches.read(switches.FORCE_HASH_JOINS, {}) is True


@pytest.mark.parametrize("text", ["1", "true", "TRUE", "yes", "on", " on "])
def test_the_spellings_of_on(text):
    assert switches.read(switches.FORCE_HASH_JOINS, {"DJANGO_OVERLAY_FORCE_HASH_JOINS": text})


@pytest.mark.parametrize("text", ["0", "false", "no", "off", "", "  "])
def test_everything_else_is_off(text):
    """Including the empty string. `FOO=` in a shell script is a fumbled set,
    and off is the safer reading of the two."""
    assert not switches.read(switches.FORCE_HASH_JOINS, {"DJANGO_OVERLAY_FORCE_HASH_JOINS": text})


def test_a_bool_is_returned_not_the_string_that_carried_it():
    """The library raises ImproperlyConfigured for a non-bool, which would fire
    here first and read as its bug rather than the harness's."""
    value = switches.read(switches.FORCE_HASH_JOINS, {"DJANGO_OVERLAY_FORCE_HASH_JOINS": "yes"})
    assert value is True


def test_nothing_given_leaves_every_switch_on():
    assert switches.resolve({}) == {
        switches.option_name(s): True for s in switches.SWITCHES
    }


def test_no_optimisations_turns_all_four_off():
    assert switches.resolve({}, all_off=True) == {
        switches.option_name(s): False for s in switches.SWITCHES
    }


def test_one_switch_can_be_lifted_back_out_of_no_optimisations():
    """The useful combination: what is one optimisation worth on its own, as
    opposed to what are the four worth together."""
    chosen = switches.resolve({"force_hash_joins": True}, all_off=True)
    assert chosen["force_hash_joins"] is True
    assert chosen["rewrite_traversals"] is False


def test_one_switch_can_be_dropped_out_of_the_default():
    chosen = switches.resolve({"force_hash_joins": False})
    assert chosen["force_hash_joins"] is False
    assert chosen["rewrite_traversals"] is True


def test_applying_writes_the_names_settings_reads():
    environ = {}
    switches.apply(switches.resolve({"force_hash_joins": False}), environ)
    assert environ["DJANGO_OVERLAY_FORCE_HASH_JOINS"] == "0"
    assert environ["DJANGO_OVERLAY_REWRITE_TRAVERSALS"] == "1"
    # And reads back as what was asked for, which is the round trip settings.py
    # actually performs.
    expected = dict.fromkeys((switches.option_name(s) for s in switches.SWITCHES), True)
    expected["force_hash_joins"] = False
    assert switches.state(environ) == expected


def test_the_off_switches_are_named_the_way_they_were_typed():
    """`force-hash-joins`, not `force_hash_joins`. It goes into a message
    telling somebody which flag produced the numbers they are looking at."""
    assert switches.describe({"force_hash_joins": False, "rewrite_traversals": True}) == [
        "force-hash-joins"
    ]


def test_the_record_is_read_from_settings_not_from_the_environment():
    """What the library obeyed, not what the CLI asked for. The gap between
    those two is exactly the plumbing bug worth catching."""
    class Settings:
        DJANGO_OVERLAY_FORCE_HASH_JOINS = False

    recorded = switches.configured(Settings())
    assert recorded["force_hash_joins"] is False
    # Absent from the settings object means on, same as the library reads it.
    assert recorded["rewrite_traversals"] is True


# -------------------------------- comparing across arms rather than machines


def test_two_arms_of_the_same_run_are_still_comparable():
    """Turning an optimisation off is the measurement, not a confound. If it
    landed in COMPARABILITY_KEYS the A/B would suppress its own result."""
    on = an_environment(switches={"force_hash_joins": True})
    off = an_environment(switches={"force_hash_joins": False})
    assert environment.comparable(on, off)


def test_a_switch_difference_is_named_in_the_note():
    """A +21950% column from a flag looks exactly like one from a regression."""
    baseline = {
        "label": "all-on", "saved_at": "now",
        "environment": an_environment(switches={"rewrite_traversals": True}),
    }
    note = results.comparison_note(
        baseline, an_environment(switches={"rewrite_traversals": False}))
    assert note.startswith("delta column")   # the runner tests this prefix
    # Named the way it was typed, matching the summary line.
    assert "rewrite-traversals on -> off" in note
    assert "not a code change" in note


def test_matching_arms_add_nothing_to_the_note():
    baseline = {
        "label": "all-on", "saved_at": "now",
        "environment": an_environment(switches={"rewrite_traversals": True}),
    }
    note = results.comparison_note(
        baseline, an_environment(switches={"rewrite_traversals": True}))
    assert "optimisation change" not in note


def test_a_run_saved_before_switches_existed_reads_as_all_on():
    """Absent means on everywhere else; a saved run with no switches key must
    not report four differences against a fresh all-on run."""
    assert environment.switch_differences(
        an_environment(), an_environment(switches=switches.resolve({}))) == []


def test_the_summary_line_says_which_optimisations_were_off():
    env = an_environment(
        postgres_version="17.2", passes=2,
        switches={"force_hash_joins": False, "rewrite_traversals": True},
    )
    line = environment.summarise(env)
    assert "optimisations OFF: force-hash-joins" in line


def test_the_summary_line_stays_quiet_when_nothing_is_off():
    env = an_environment(postgres_version="17.2", passes=2,
                         switches=switches.resolve({}))
    assert "OFF" not in environment.summarise(env)


# ------------------------------------------------------- the saved run's name


def test_both_arms_of_an_ab_get_different_default_labels():
    """Otherwise the second run overwrites the first and the comparison is
    against itself -- the git sha is the same string for both arms."""
    from benchmark.cli import _default_label

    on = {"git_sha": "abc1234", "switches": switches.resolve({})}
    off = {"git_sha": "abc1234", "switches": switches.resolve({}, all_off=True)}
    assert _default_label(on) == "abc1234"
    assert _default_label(off) == "abc1234-no-optimisations"
    assert _default_label(on) != _default_label(off)


def test_a_single_switch_off_is_named_in_the_label():
    from benchmark.cli import _default_label

    label = _default_label(
        {"git_sha": "abc1234", "switches": switches.resolve({"force_hash_joins": False})})
    assert label == "abc1234-no-force-hash-joins"


# ------------------------------------- and does any of it reach the library?
#
# Everything above tests the wiring in pieces. This tests the whole run of it,
# because the pieces were once all correct and the effect still did not happen:
# `benchmark/suites/ban.py` set the threshold on the models package while the
# module that read it had the name bound in its own namespace, so the suite
# compared the unbanned query against itself for a whole branch, and passed.
# The only check that would have caught it is the one that asks the library.
#
# A subprocess per arm, because settings.py reads the environment at import
# time and django.setup() happens once per process. That is also the property
# under test -- resolve the flags after setup and they move nothing.

PROBE = """
import json, sys
from click.testing import CliRunner

from benchmark.cli import benchmark

outcome = CliRunner().invoke(benchmark, sys.argv[1:] + ["--list-suites"])
if outcome.exit_code != 0:
    raise SystemExit(f"{outcome.output}\\n{outcome.exception!r}")

from django.conf import settings

from django_overlay import fields
from django_overlay.models import planning, query, queryset

print("PROBE=" + json.dumps({
    "rewrite_traversals": query._rewrite_traversals_enabled(),
    "redirect_select_related": queryset._redirect_select_related_enabled(),
    "force_hash_joins": planning._force_hash_joins_enabled(),
    "array_subquery_in": fields._array_subquery_in_enabled(),
}))
print("SETTINGS=" + settings.SETTINGS_MODULE)
"""


def what_the_library_saw(*flags, want_settings_module=False):
    """Run the real command with these flags, then ask the library itself."""
    import json
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parent.parent
    finished = subprocess.run(
        [sys.executable, "-c", PROBE, *flags],
        cwd=root, capture_output=True, text=True, timeout=180, check=False,
    )
    assert finished.returncode == 0, finished.stdout + finished.stderr
    lines = finished.stdout.splitlines()
    line = next(text for text in lines if text.startswith("PROBE="))
    if want_settings_module:
        return next(text for text in lines if text.startswith("SETTINGS=")).removeprefix("SETTINGS=")
    return json.loads(line.removeprefix("PROBE="))


def test_by_default_the_library_has_every_optimisation_on():
    assert what_the_library_saw() == {
        "rewrite_traversals": True, "redirect_select_related": True,
        "force_hash_joins": True, "array_subquery_in": True,
    }


def test_no_optimisations_reaches_all_four_gates_in_the_library():
    assert what_the_library_saw("--no-optimisations") == {
        "rewrite_traversals": False, "redirect_select_related": False,
        "force_hash_joins": False, "array_subquery_in": False,
    }


@pytest.mark.parametrize("flag", [
    "--no-rewrite-traversals",
    "--no-redirect-select-related",
    "--no-force-hash-joins",
    "--no-array-subquery-in",
])
def test_each_flag_turns_off_its_own_gate_and_no_other(flag):
    """One flag reaching the wrong setting is the mistake a table of names
    invites, and it produces a plausible number rather than a failure."""
    seen = what_the_library_saw(flag)
    expected = flag.removeprefix("--no-").replace("-", "_")
    assert seen[expected] is False
    assert [name for name, on in seen.items() if not on] == [expected]


def test_the_benchmark_settings_module_wins_over_an_exported_one():
    """This test runs with DJANGO_SETTINGS_MODULE=tests.django_settings
    exported, which is also the normal state of a Django developer's shell.
    It used to win, and then every flag above moved a setting nothing read --
    the run measured the default arm and filed it under the other arm's name.
    """
    assert what_the_library_saw(want_settings_module=True) == "benchmark.settings"


# ------------------------------------- a run that half failed is not a baseline
#
# A lost connection already gets its own cell state, because "this query is too
# slow" and "this query never ran" are different claims. What it did not have
# was any consequence beyond that cell: the run still printed a table, still
# saved itself under the default label, and the next run would have picked it up
# as a baseline automatically. Observed at scale 1.0, where the compose database
# ran out of /dev/shm and eleven cells came back unmeasured.


def a_run(label="before", lost=0, **environment_overrides):
    return {
        "label": label,
        "saved_at": f"2026-01-0{1 + lost}",
        "lost": lost,
        "environment": an_environment(**environment_overrides),
        "suites": [],
    }


def test_a_run_with_unmeasured_cells_is_not_picked_up_automatically(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "DIRECTORY", tmp_path)
    results.save("clean", an_environment(), [], lost=0)
    results.save("broken", an_environment(), [], lost=11)

    picked = results.latest()
    assert picked is not None
    assert picked["label"] == "clean", "an incomplete run must not become the baseline"


def test_it_is_still_returned_when_named(tmp_path, monkeypatch):
    """Consent is the distinction. Typing --compare-to says which run you mean;
    a delta column nobody asked for is the one that must not be built on it."""
    monkeypatch.setattr(results, "DIRECTORY", tmp_path)
    results.save("broken", an_environment(), [], lost=11)

    assert results.latest("broken")["label"] == "broken"


def test_no_usable_baseline_is_the_same_as_no_baseline(tmp_path, monkeypatch):
    """With nothing but incomplete runs saved, later runs get no delta column
    rather than a delta against one of them."""
    monkeypatch.setattr(results, "DIRECTORY", tmp_path)
    results.save("broken", an_environment(), [], lost=3)

    assert results.latest() is None
    assert results.comparison_note(None, an_environment()) is None


def test_the_count_survives_the_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "DIRECTORY", tmp_path)
    path = results.save("broken", an_environment(), [], lost=11)
    assert json.loads(path.read_text())["lost"] == 11


def test_a_run_saved_before_the_count_existed_is_treated_as_clean(tmp_path, monkeypatch):
    """`lost` is absent from every run saved so far, and absent has to mean
    usable -- otherwise adding this guard would silently discard every existing
    baseline on disk."""
    monkeypatch.setattr(results, "DIRECTORY", tmp_path)
    (tmp_path / "old.json").write_text(json.dumps({
        "label": "old", "saved_at": "2026-01-01",
        "environment": an_environment(), "suites": [],
    }))

    assert results.latest()["label"] == "old"


def test_the_lost_note_is_one_string_in_one_place():
    """runner.py counts cells by this note and harness.py writes it. A literal
    repeated in both is one rename away from a guard that matches nothing."""
    assert harness.Cell(0.0, note=harness.LOST_NOTE).note == harness.LOST_NOTE
    assert harness.LOST_NOTE == "conn lost"


def a_saved_shape(*notes):
    """A run in the shape section_to_data produces, one cell per note."""
    return [{
        "name": "s", "sections": [{
            "title": "t", "columns": ["overlay"], "note": "",
            "rows": [
                {"label": f"row{index}", "cells": {"overlay": {
                    "ms": 0.0, "capped": False, "note": note}}, "extras": {}}
                for index, note in enumerate(notes)
            ],
        }],
    }]


def test_unmeasured_cells_are_counted_and_measured_ones_are_not():
    assert harness.lost_cells(a_saved_shape(harness.LOST_NOTE, "", None)) == 1
    assert harness.lost_cells(a_saved_shape(harness.LOST_NOTE, harness.LOST_NOTE)) == 2
    assert harness.lost_cells(a_saved_shape("", None, "skipped")) == 0


def test_a_run_with_no_sections_counts_nothing():
    """A suite skipped for budget yields no sections, which is not a loss."""
    assert harness.lost_cells([{"name": "s", "sections": []}]) == 0
    assert harness.lost_cells([]) == 0


def test_the_count_matches_what_a_real_section_serialises_to():
    """Counted off section_to_data's output, so the two cannot drift: the keys
    here are the keys it writes, not the keys this test hopes it writes."""
    section = harness.Section(
        title="t", columns=["overlay"], note="",
        rows=[harness.Row(label="r", cells={"overlay": harness.Cell(0.0, note=harness.LOST_NOTE)})],
    )
    run = [{"name": "s", "sections": [harness.section_to_data(section)]}]
    assert harness.lost_cells(run) == 1
