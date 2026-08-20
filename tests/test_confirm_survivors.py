"""Phase two decides what the mutation policy actually gates on.

Phase one only runs the tests mutmut's tracing picked, so its survivors are a
superset of the real ones. .github/scripts/confirm_survivors.py re-runs each
against the whole suite, and the build passes or fails on what it says. That
makes two things worth testing here rather than only in a six-hour CI job: that
a mutant killed by the full suite is not reported as surviving, and that the
ways this can go quietly wrong -- a shard that never reported, a phase-one run
that tested nothing -- come out as failures rather than as silence.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def load_script():
    """Import by path -- .github/scripts/ is not an importable package."""
    path = ROOT / ".github" / "scripts" / "confirm_survivors.py"
    spec = importlib.util.spec_from_file_location("confirm_survivors", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("confirm_survivors", module)
    spec.loader.exec_module(module)
    return module


confirm_survivors = load_script()


def write_meta(root, module, codes):
    path = root / "mutants" / "django_overlay"
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{module}.py.meta").write_text(json.dumps({"exit_code_by_key": codes}))


# ------------------------------------------------- which mutants need settling


def test_only_mutants_that_lived_are_carried_into_phase_two(tmp_path):
    write_meta(tmp_path, "sql", {
        "django_overlay.sql.x_a__mutmut_1": 1,    # killed
        "django_overlay.sql.x_b__mutmut_1": 3,    # killed: pytest internal error
        "django_overlay.sql.x_c__mutmut_1": -24,  # killed
        "django_overlay.sql.x_d__mutmut_1": 0,    # survived
        "django_overlay.sql.x_e__mutmut_1": 33,   # no tests -- alive
    })
    alive, unchecked = confirm_survivors.alive_from_meta(tmp_path / "mutants")
    assert alive == ["django_overlay.sql.x_d__mutmut_1", "django_overlay.sql.x_e__mutmut_1"]
    assert unchecked == []


def test_a_pragma_skipped_mutant_is_not_a_survivor(tmp_path):
    """`# pragma: no mutate` lines are the one benign bucket."""
    write_meta(tmp_path, "sql", {"django_overlay.sql.x_a__mutmut_1": 34})
    alive, _ = confirm_survivors.alive_from_meta(tmp_path / "mutants")
    assert alive == []


def test_a_mutant_with_no_verdict_is_reported_separately(tmp_path):
    """Not checked is not the same as not killed, and must not be confirmed."""
    write_meta(tmp_path, "sql", {"django_overlay.sql.x_a__mutmut_1": None})
    alive, unchecked = confirm_survivors.alive_from_meta(tmp_path / "mutants")
    assert alive == []
    assert unchecked == ["django_overlay.sql.x_a__mutmut_1"]


def test_unreadable_meta_does_not_stop_the_others(tmp_path):
    write_meta(tmp_path, "sql", {"django_overlay.sql.x_a__mutmut_1": 0})
    (tmp_path / "mutants" / "django_overlay" / "broken.py.meta").write_text("{not json")
    alive, _ = confirm_survivors.alive_from_meta(tmp_path / "mutants")
    assert alive == ["django_overlay.sql.x_a__mutmut_1"]


# --------------------------------------------------------- the verdicts

def test_the_full_suite_passing_means_the_mutant_really_survives():
    outcome = confirm_survivors.confirm(
        ["a"], run=lambda name: (0, 31.0, None), say=lambda *_: None, cache={}, hashes={}
    )
    assert (outcome.confirmed, outcome.killed, outcome.hung) == (["a"], [], [])


def test_the_full_suite_failing_kills_a_phase_one_survivor():
    """The whole point: tracing missed the test, the full suite has it."""
    outcome = confirm_survivors.confirm(
        ["a"], run=lambda name: (1, 12.0, "tests/test_x.py::test_y"),
        say=lambda *_: None, cache={}, hashes={}
    )
    assert (outcome.confirmed, outcome.killed, outcome.hung) == ([], ["a"], [])


def test_a_mutant_with_no_verdict_in_time_is_not_a_pass():
    outcome = confirm_survivors.confirm(
        ["a"], run=lambda name: (None, 900.0, None), say=lambda *_: None, cache={}, hashes={}
    )
    assert (outcome.confirmed, outcome.killed, outcome.hung) == ([], [], ["a"])


def test_progress_names_each_mutant_as_it_is_settled():
    """A step with no output for an hour is indistinguishable from a hung one."""
    said = []
    confirm_survivors.confirm(["a", "b"], run=lambda name: (0, 1.0, None),
                              say=said.append, cache={}, hashes={})
    assert len(said) == 2
    assert "[2/2]" in said[1] and "b" in said[1]


# ------------------------------------------------------------ the exit code

@pytest.fixture
def run_in(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_shard_with_no_survivors_passes(run_in, monkeypatch):
    write_meta(run_in, "sql", {"django_overlay.sql.x_a__mutmut_1": 1})
    assert confirm_survivors.main(["--label", "ddl"]) == 0


def test_a_confirmed_survivor_fails_the_build(run_in, monkeypatch):
    write_meta(run_in, "sql", {"django_overlay.sql.x_a__mutmut_1": 0})
    monkeypatch.setattr(confirm_survivors, "run_full_suite",
                        lambda name, timeout=0: (0, 1.0, None))
    assert confirm_survivors.main(["--label", "ddl"]) == 1
    report = json.loads((run_in / "mutants" / "mutmut-confirmed.json").read_text())
    assert report["confirmed"] == ["django_overlay.sql.x_a__mutmut_1"]
    assert report["label"] == "ddl"


def test_a_survivor_the_full_suite_kills_does_not_fail_the_build(run_in, monkeypatch):
    write_meta(run_in, "sql", {"django_overlay.sql.x_a__mutmut_1": 0})
    # Clean with no mutant, red with it: a test the tracing missed.
    monkeypatch.setattr(confirm_survivors, "run_full_suite",
                        lambda name, timeout=0: (0, 1.0, None) if name == ""
                        else (1, 1.0, "tests/test_x.py::test_y"))
    assert confirm_survivors.main(["--label", "ddl"]) == 0
    report = json.loads((run_in / "mutants" / "mutmut-confirmed.json").read_text())
    assert report["confirmed"] == []
    assert report["killed_by_full_suite"] == ["django_overlay.sql.x_a__mutmut_1"]


def test_a_phase_one_that_tested_nothing_is_a_failure_not_a_pass(run_in):
    """The green-report failure mode, one phase later."""
    write_meta(run_in, "sql", {"django_overlay.sql.x_a__mutmut_1": None})
    assert confirm_survivors.main(["--label", "ddl"]) == 1


# ------------------------------------------------------------- the baseline
#
# A verdict here reads "the suite failed, so the mutant is dead". A suite that
# fails for its own reasons fails that way for every mutant, which would report
# every survivor as killed and turn the build green having confirmed nothing.


def test_the_baseline_runs_with_no_mutant_active():
    asked = []
    confirm_survivors.baseline_is_clean(run=lambda name: asked.append(name) or (0, 1.0, None),
                                        say=lambda *_: None)
    assert asked == [""], "the baseline has to be the unmutated suite"


def test_a_dirty_baseline_stops_phase_two_before_it_confirms_anything(run_in, monkeypatch):
    write_meta(run_in, "sql", {"django_overlay.sql.x_a__mutmut_1": 0})
    tried = []

    def never_passes(name, timeout=0):
        tried.append(name)
        return 1, 1.0, "tests/test_x.py::test_y"

    monkeypatch.setattr(confirm_survivors, "run_full_suite", never_passes)
    assert confirm_survivors.main(["--label", "ddl"]) == 1
    assert tried == [""], "it must not confirm anything against a suite that is already red"


def test_a_clean_baseline_lets_the_confirmations_run(run_in, monkeypatch):
    write_meta(run_in, "sql", {"django_overlay.sql.x_a__mutmut_1": 0})
    monkeypatch.setattr(confirm_survivors, "run_full_suite",
                        lambda name, timeout=0: (0, 1.0, None))
    assert confirm_survivors.main(["--label", "ddl"]) == 1  # the survivor is real
    report = json.loads((run_in / "mutants" / "mutmut-confirmed.json").read_text())
    assert report["confirmed"] == ["django_overlay.sql.x_a__mutmut_1"]


def test_nothing_to_confirm_costs_no_baseline_pass(run_in, monkeypatch):
    """A shard where everything died in phase one should not run the suite at all."""
    write_meta(run_in, "sql", {"django_overlay.sql.x_a__mutmut_1": 1})
    monkeypatch.setattr(confirm_survivors, "run_full_suite",
                        lambda name, timeout=0: pytest.fail("ran the suite for nothing"))
    assert confirm_survivors.main(["--label", "ddl"]) == 0


# ------------------------------------------------------- reusing a verdict
#
# Phase two costs a full suite pass per survivor, and a shard would pay that
# again on every push. A cached kill is only reusable while the thing that
# produced it still holds: the mutated function, and the test that did the
# killing. A cached survivor is never reused at all -- see reusable().


def cached(name="a", killed_by="tests/test_x.py::test_y", function_hash="fn", file_hash="fh"):
    return {name: {"killed_by": killed_by, "function_hash": function_hash,
                   "test_file_hash": file_hash}}


@pytest.fixture
def a_test_file(tmp_path):
    """A stand-in for the test that did the killing, hashable on disk."""
    path = tmp_path / "tests" / "test_x.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def test_y(): pass\n")
    return path


def run_and_count(names, cache, hashes, root, counter):
    def run(name):
        counter.append(name)
        return 1, 1.0, "tests/test_x.py::test_y"

    return confirm_survivors.confirm(names, run=run, say=lambda *_: None,
                                     cache=cache, hashes=hashes, root=str(root))


def test_a_reused_survivor_still_counts_as_surviving(tmp_path, a_test_file):
    """The cache must not launder a survivor into a kill."""
    entry = {"a": {"killed_by": None, "function_hash": "fn",
                   "suite_hash": confirm_survivors.suite_hash(str(tmp_path))}}
    outcome = run_and_count(["a"], entry, {"a": "fn"}, tmp_path, [])
    assert outcome.confirmed == ["a"] and outcome.killed == []


def test_an_unchanged_kill_is_not_re_run(tmp_path, a_test_file):
    ran = []
    outcome = run_and_count(
        ["a"], cached(file_hash=confirm_survivors.sha(a_test_file)),
        {"a": "fn"}, tmp_path, ran,
    )
    assert ran == [], "it re-ran a verdict nothing had invalidated"
    assert outcome.killed == ["a"] and outcome.reused == ["a"]


def test_a_kill_is_re_run_when_the_mutated_function_changes(tmp_path, a_test_file):
    ran = []
    run_and_count(["a"], cached(file_hash=confirm_survivors.sha(a_test_file)),
                  {"a": "a different hash"}, tmp_path, ran)
    assert ran == ["a"]


def test_a_kill_is_re_run_when_the_test_that_killed_it_changes(tmp_path, a_test_file):
    """Weaken the test and the verdict it produced stops being evidence."""
    ran = []
    stale = cached(file_hash=confirm_survivors.sha(a_test_file))
    a_test_file.write_text("def test_y(): assert True  # weakened\n")
    run_and_count(["a"], stale, {"a": "fn"}, tmp_path, ran)
    assert ran == ["a"]


def test_a_kill_is_re_run_when_the_test_that_killed_it_is_deleted(tmp_path, a_test_file):
    ran = []
    stale = cached(file_hash=confirm_survivors.sha(a_test_file))
    a_test_file.unlink()
    run_and_count(["a"], stale, {"a": "fn"}, tmp_path, ran)
    assert ran == ["a"]


def test_a_survivor_is_reused_while_no_test_has_changed(tmp_path, a_test_file):
    """"Nothing kills it" stays true until some test changes."""
    ran = []
    entry = {"a": {"killed_by": None, "function_hash": "fn",
                   "suite_hash": confirm_survivors.suite_hash(str(tmp_path))}}
    outcome = run_and_count(["a"], entry, {"a": "fn"}, tmp_path, ran)
    assert ran == []
    assert outcome.reused == ["a"]


def test_a_survivor_is_re_checked_when_any_test_changes(tmp_path, a_test_file):
    """It is a claim about the whole suite, so the whole suite invalidates it."""
    ran = []
    entry = {"a": {"killed_by": None, "function_hash": "fn",
                   "suite_hash": confirm_survivors.suite_hash(str(tmp_path))}}
    (tmp_path / "tests" / "test_new.py").write_text("def test_z(): pass\n")
    run_and_count(["a"], entry, {"a": "fn"}, tmp_path, ran)
    assert ran == ["a"]


def test_a_survivor_is_re_checked_when_the_mutated_function_changes(tmp_path, a_test_file):
    ran = []
    entry = {"a": {"killed_by": None, "function_hash": "fn",
                   "suite_hash": confirm_survivors.suite_hash(str(tmp_path))}}
    run_and_count(["a"], entry, {"a": "changed"}, tmp_path, ran)
    assert ran == ["a"]


def test_a_survivor_with_no_suite_hash_is_re_checked(tmp_path):
    """Entries written before survivors were cacheable must not be trusted."""
    ran = []
    run_and_count(["a"], {"a": {"killed_by": None, "function_hash": "fn"}},
                  {"a": "fn"}, tmp_path, ran)
    assert ran == ["a"]


def test_a_kill_nobody_can_attribute_is_not_cached(tmp_path):
    outcome = confirm_survivors.confirm(
        ["a"], run=lambda name: (1, 1.0, None), say=lambda *_: None,
        cache={}, hashes={}, root=str(tmp_path),
    )
    assert outcome.killed == ["a"]
    assert outcome.verdicts == {}, "a verdict with no test behind it cannot be pinned to one"


def test_the_killing_test_is_read_out_of_pytest_output():
    output = (
        "..F\n=== short test summary info ===\n"
        "FAILED tests/test_soft_delete.py::test_unique_together - TypeError\n"
    )
    assert confirm_survivors.killing_test(output) == "tests/test_soft_delete.py::test_unique_together"


def test_output_with_no_failure_names_no_test():
    assert confirm_survivors.killing_test("884 passed\n") is None


def test_a_corrupt_cache_is_ignored_rather_than_fatal(tmp_path):
    (tmp_path / "cache.json").write_text("{not json")
    assert confirm_survivors.read_cache(tmp_path / "cache.json") == {}


def test_a_cache_from_an_older_format_is_ignored(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"version": 0, "verdicts": cached()}))
    assert confirm_survivors.read_cache(path) == {}


def test_the_cache_survives_the_round_trip(run_in, monkeypatch, a_test_file):
    """What one run writes, the next has to be able to reuse."""
    write_meta(run_in, "sql", {"django_overlay.sql.x_a__mutmut_1": 0})
    meta = run_in / "mutants" / "django_overlay" / "sql.py.meta"
    meta.write_text(json.dumps({
        "exit_code_by_key": {"django_overlay.sql.x_a__mutmut_1": 0},
        "hash_by_function_name": {"x_a": "fn"},
    }))
    (run_in / "mutants" / "tests").mkdir(parents=True, exist_ok=True)
    (run_in / "mutants" / "tests" / "test_x.py").write_text("def test_y(): pass\n")
    monkeypatch.setattr(
        confirm_survivors, "run_full_suite",
        lambda name, timeout=0: (0, 1.0, None) if name == ""
        else (1, 1.0, "tests/test_x.py::test_y"),
    )
    assert confirm_survivors.main(["--label", "ddl"]) == 0

    written = confirm_survivors.read_cache(run_in / "mutants" / "mutmut-confirmed-cache.json")
    assert confirm_survivors.reusable(
        written["django_overlay.sql.x_a__mutmut_1"],
        "django_overlay.sql.x_a__mutmut_1",
        confirm_survivors.function_hashes(str(run_in / "mutants")),
        root=str(run_in / "mutants"),
    )


# --------------------------------------------------------------- the union

def write_report(root, shard, confirmed=(), killed=(), hung=()):
    path = root / f"mutmut-stats-{shard}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "mutmut-confirmed.json").write_text(json.dumps({
        "label": shard, "confirmed": list(confirmed),
        "killed_by_full_suite": list(killed), "hung": list(hung), "unchecked": [],
    }))


def all_shards():
    return sorted(confirm_survivors.expected_shards())


def test_the_union_passes_only_when_every_shard_is_clean(tmp_path):
    for shard in all_shards():
        write_report(tmp_path, shard, killed=["x"])
    assert confirm_survivors.main(
        ["--aggregate", str(tmp_path), "--expect-every-shard"]
    ) == 0


def test_one_survivor_anywhere_fails_the_union(tmp_path):
    for shard in all_shards():
        write_report(tmp_path, shard)
    write_report(tmp_path, all_shards()[0], confirmed=["django_overlay.sql.x_a__mutmut_1"])
    assert confirm_survivors.main(
        ["--aggregate", str(tmp_path), "--expect-every-shard"]
    ) == 1


def test_a_shard_that_never_reported_fails_the_union(tmp_path):
    """Five green shards and a missing one must not read as no survivors."""
    for shard in all_shards()[1:]:
        write_report(tmp_path, shard)
    assert confirm_survivors.main(
        ["--aggregate", str(tmp_path), "--expect-every-shard"]
    ) == 1


def test_an_unreadable_report_fails_the_union(tmp_path):
    for shard in all_shards():
        write_report(tmp_path, shard)
    broken = tmp_path / f"mutmut-stats-{all_shards()[0]}" / "mutmut-confirmed.json"
    broken.write_text("{not json")
    assert confirm_survivors.main(
        ["--aggregate", str(tmp_path), "--expect-every-shard"]
    ) == 1


def test_a_hung_mutant_fails_the_union_too(tmp_path):
    for shard in all_shards():
        write_report(tmp_path, shard)
    write_report(tmp_path, all_shards()[0], hung=["django_overlay.sql.x_a__mutmut_1"])
    assert confirm_survivors.main(
        ["--aggregate", str(tmp_path), "--expect-every-shard"]
    ) == 1


def test_the_names_of_survivors_reach_the_summary(tmp_path):
    """A count tells nobody which mutant to go and kill."""
    write_report(tmp_path, all_shards()[0], confirmed=["django_overlay.sql.x_a__mutmut_1"])
    summary = tmp_path / "summary.md"
    confirm_survivors.main(["--aggregate", str(tmp_path), "--summary", str(summary)])
    assert "django_overlay.sql.x_a__mutmut_1" in summary.read_text()


def test_every_shard_the_workflow_runs_can_be_confirmed():
    """The union check reads the shard map; the workflow has to dispatch the same set."""
    workflow = (ROOT / ".github" / "workflows" / "mutation.yml").read_text()
    assert "confirm_survivors.py" in workflow
    for shard in confirm_survivors.expected_shards():
        assert shard in workflow, f"{shard} is in the shard map but not in mutation.yml"
