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

import time

import pytest
from django.db import OperationalError

from benchmark import environment, estimates, harness, results
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
