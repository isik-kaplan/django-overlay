"""The step that keeps a restored mutation cache from outliving the tests behind it.

What it guards against is a false green: a mutant recorded as killed by a test
that has since been weakened or deleted, reused from cache and never checked
again. Every case here builds a small project in a temporary directory -- a
pyproject.toml, a tests/ tree, and a mutants/ tree shaped like the one mutmut
leaves -- and runs the step against it the way CI does, from the project root.
"""

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def load_script():
    """Import by path -- .github/scripts/ is not an importable package."""
    path = ROOT / ".github" / "scripts" / "invalidate_mutation_cache.py"
    spec = importlib.util.spec_from_file_location("invalidate_mutation_cache", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("invalidate_mutation_cache", module)
    spec.loader.exec_module(module)
    return module


cache = load_script()

PYPROJECT = """
[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.mutmut]
source_paths = ["pkg/"]
also_copy = ["tests/", "pkg/templates/", ".github/", "pyproject.toml"]
"""

SQL = "pkg.sql.x_render__mutmut_1"
CLI = "pkg.cli.x_main__mutmut_1"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project whose cache was written against the suite as it stands."""
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "pyproject.toml", PYPROJECT)
    write(tmp_path / "pkg" / "sql.py", "def render(): ...\n")
    write(tmp_path / "pkg" / "cli.py", "def main(): ...\n")
    write(tmp_path / "pkg" / "templates" / "q.sql.j2", "SELECT 1\n")
    write(tmp_path / "tests" / "conftest.py", "")
    write(tmp_path / "tests" / "test_sql.py", "def test_render(): ...\n")
    write(tmp_path / "tests" / "test_cli.py", "def test_main(): ...\n")
    write(tmp_path / "tests" / "sub" / "conftest.py", "")
    write(tmp_path / "tests" / "sub" / "test_deep.py", "def test_deep(): ...\n")
    write(tmp_path / "tests" / "support.py", "")
    write(tmp_path / ".github" / "scripts" / "x.py", "")
    # The tree mutmut built: mirrors of every copied path, plus its own files.
    for path in ["pkg/sql.py", "pkg/cli.py", "pkg/templates/q.sql.j2", "tests/test_sql.py", "tests/test_cli.py"]:
        write(tmp_path / "mutants" / path, (tmp_path / path).read_text())
    write_stats(
        {
            "pkg.sql.x_render": ["tests/test_sql.py::test_render", "tests/sub/test_deep.py::test_deep"],
            "pkg.cli.x_main": ["tests/test_cli.py::test_main"],
        }
    )
    write_verdicts(sql={SQL: 1}, cli={CLI: 1})
    # The baseline that cache was built against, as the previous run wrote it.
    baseline = cache.snapshot()
    write(tmp_path / "mutants" / ".suite-hashes.json", json.dumps(baseline))
    return tmp_path


def write_stats(traces):
    tests = sorted({test for names in traces.values() for test in names})
    stats = {
        "tests_by_mangled_function_name": traces,
        "duration_by_test": {test: 0.1 for test in tests},
        "stats_time": 1.0,
        "function_hashes": {},
        "function_dependencies": {},
        "config_fingerprint": {},
        "watched_file_hashes": {"uv.lock": "abc"},
        "git_commit": "deadbeef",
    }
    write(Path("mutants") / "mutmut-stats.json", json.dumps(stats))


def read_stats():
    return json.loads((Path("mutants") / "mutmut-stats.json").read_text())


def write_verdicts(**codes_by_module):
    for module, codes in codes_by_module.items():
        meta = {
            "exit_code_by_key": codes,
            "hash_by_function_name": {},
            "type_check_error_by_key": {},
            "durations_by_key": {},
            "estimated_durations_by_key": {},
        }
        write(Path("mutants") / "pkg" / f"{module}.py.meta", json.dumps(meta))


def verdict(module, name):
    return json.loads((Path("mutants") / "pkg" / f"{module}.py.meta").read_text())["exit_code_by_key"][name]


def run():
    said = []
    outcome = cache.invalidate(say=said.append)
    return outcome, said


# ------------------------------------------------------------ what changed


def test_an_unchanged_suite_keeps_every_verdict(project):
    outcome, _ = run()
    assert outcome["changed"] == 0
    assert verdict("sql", SQL) == 1
    assert verdict("cli", CLI) == 1


def test_an_edited_test_resets_the_kills_that_rested_on_it_and_no_others(project):
    write(project / "tests" / "test_sql.py", "def test_render(): pass  # weakened\n")
    run()
    assert verdict("sql", SQL) is None
    assert verdict("cli", CLI) == 1


def test_a_deleted_test_resets_the_kills_that_rested_on_it(project):
    (project / "tests" / "test_sql.py").unlink()
    run()
    assert verdict("sql", SQL) is None
    assert verdict("cli", CLI) == 1


def test_a_conftest_resets_kills_resting_on_any_test_beneath_it(project):
    # It owns no test id, so matching changed paths against ids finds nothing
    # -- which is how the first version of this in openfield shipped, and a
    # fixture edit kept every kill.
    write(project / "tests" / "sub" / "conftest.py", "import pytest\n")
    run()
    assert verdict("sql", SQL) is None  # traced to tests/sub/test_deep.py
    assert verdict("cli", CLI) == 1


def test_a_helper_module_resets_everything(project):
    write(project / "tests" / "support.py", "VALUE = 2\n")
    outcome, said = run()
    assert verdict("sql", SQL) is None
    assert verdict("cli", CLI) is None
    assert "everything" in said[0]


def test_a_sql_template_resets_everything(project):
    # Behaviour mutmut cannot hash: the function rendering it is unchanged.
    write(project / "pkg" / "templates" / "q.sql.j2", "SELECT 2\n")
    run()
    assert verdict("sql", SQL) is None
    assert verdict("cli", CLI) is None


def test_a_pytest_config_change_resets_everything(project):
    text = (project / "pyproject.toml").read_text()
    write(project / "pyproject.toml", text.replace('testpaths = ["tests"]', 'testpaths = ["tests"]\naddopts = "-x"'))
    run()
    assert verdict("cli", CLI) is None


def test_selecting_a_shard_is_not_a_change(project):
    # CI rewrites [tool.mutmut] on every run to pick the shard.
    text = (project / "pyproject.toml").read_text()
    write(project / "pyproject.toml", text.replace("[tool.mutmut]", '[tool.mutmut]\nonly_mutate = ["pkg/sql.py"]'))
    outcome, _ = run()
    assert outcome["changed"] == 0


def test_the_mutation_tooling_is_not_watched(project):
    write(project / ".github" / "scripts" / "x.py", "CHANGED = True\n")
    outcome, _ = run()
    assert outcome["changed"] == 0


# ------------------------------------------------------- verdicts not kills


@pytest.mark.parametrize("code", [0, 33, -24, 36])  # -24 is SIGXCPU: a timeout
def test_any_change_resets_every_verdict_that_was_not_a_kill(project, code):
    """mutmut re-traces a new test but reuses a cached non-kill without asking.

    So without this, a test added to kill a survivor, or to cover a function
    recorded as having no tests, never runs against it.
    """
    write_verdicts(cli={CLI: code})
    write(project / "tests" / "test_new.py", "def test_new(): ...\n")
    run()
    assert verdict("cli", CLI) is None


def test_a_type_check_verdict_is_left_to_mutmut(project):
    write_verdicts(cli={CLI: 37})
    write(project / "tests" / "test_cli.py", "def test_main(): pass\n")
    run()
    assert verdict("cli", CLI) == 37


def test_a_new_test_leaves_kills_alone(project):
    write(project / "tests" / "test_new.py", "def test_new(): ...\n")
    run()
    assert verdict("sql", SQL) == 1


def test_an_unrun_mutant_stays_unrun(project):
    write_verdicts(cli={CLI: None})
    write(project / "tests" / "test_cli.py", "def test_main(): pass\n")
    outcome, _ = run()
    assert verdict("cli", CLI) is None
    assert outcome["verdicts"] == 0  # nothing to reset, and the sql kill rests elsewhere
    assert verdict("sql", SQL) == 1


# ------------------------------------------------------------- stats map


def test_an_edited_test_leaves_both_halves_of_the_stats_map(project):
    """Pruning only duration_by_test re-traces the test while its old edges
    survive, because mutmut merges a re-trace into what it loaded with |=."""
    write(project / "tests" / "test_sql.py", "def test_render(): pass\n")
    run()
    stats = read_stats()
    assert "tests/test_sql.py::test_render" not in stats["duration_by_test"]
    traces = stats["tests_by_mangled_function_name"].values()
    assert all("tests/test_sql.py::test_render" not in tests for tests in traces)
    assert "tests/test_cli.py::test_main" in stats["duration_by_test"]


def test_the_stats_map_keeps_mutmuts_own_change_baseline(project):
    write(project / "tests" / "support.py", "VALUE = 2\n")
    run()
    stats = read_stats()
    assert stats["duration_by_test"] == {}
    assert stats["tests_by_mangled_function_name"] == {}
    assert stats["watched_file_hashes"] == {"uv.lock": "abc"}
    assert stats["git_commit"] == "deadbeef"


def test_the_baseline_is_not_a_key_mutmut_would_choke_on(project):
    # load_stats() ends with `assert not data`.
    assert (project / "mutants" / ".suite-hashes.json").is_file()
    assert set(read_stats()) == {
        "tests_by_mangled_function_name",
        "duration_by_test",
        "stats_time",
        "function_hashes",
        "function_dependencies",
        "config_fingerprint",
        "watched_file_hashes",
        "git_commit",
    }


def test_kills_with_no_stats_map_cannot_be_trusted_after_a_change(project):
    (project / "mutants" / "mutmut-stats.json").unlink()
    write(project / "tests" / "test_cli.py", "def test_main(): pass\n")
    run()
    assert verdict("sql", SQL) is None


# ---------------------------------------------------------------- baseline


def test_a_restored_tree_with_no_baseline_trusts_nothing(project):
    (project / "mutants" / ".suite-hashes.json").unlink()
    run()
    assert verdict("sql", SQL) is None
    assert verdict("cli", CLI) is None


def test_a_cold_start_writes_the_baseline_the_run_is_about_to_build(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "pyproject.toml", PYPROJECT)
    write(tmp_path / "tests" / "test_sql.py", "")
    outcome, _ = run()
    assert outcome["verdicts"] == 0
    baseline = json.loads((tmp_path / "mutants" / ".suite-hashes.json").read_text())
    assert "tests/test_sql.py" in baseline["suite"]


def test_a_second_run_after_a_change_sees_no_change(project):
    write(project / "tests" / "test_sql.py", "def test_render(): pass\n")
    run()
    write_verdicts(sql={SQL: 1})
    outcome, _ = run()
    assert outcome["changed"] == 0
    assert verdict("sql", SQL) == 1


# ------------------------------------------------------------ orphaned tree


def test_a_deleted_test_file_leaves_the_tree(project):
    """pytest runs from inside mutants/, so a test deleted from the repo but
    not from the tree is still collected, and its ids never go obsolete."""
    (project / "tests" / "test_sql.py").unlink()
    outcome, _ = run()
    assert not (project / "mutants" / "tests" / "test_sql.py").exists()
    assert (project / "mutants" / "tests" / "test_cli.py").exists()
    assert outcome["orphans"] == 1


def test_a_deleted_module_takes_its_sidecars_with_it(project):
    write(project / "mutants" / "pkg" / "cli.py.spans", "{}")
    (project / "pkg" / "cli.py").unlink()
    run()
    assert not (project / "mutants" / "pkg" / "cli.py").exists()
    assert not (project / "mutants" / "pkg" / "cli.py.meta").exists()
    assert not (project / "mutants" / "pkg" / "cli.py.spans").exists()
    assert (project / "mutants" / "pkg" / "sql.py.meta").exists()


def test_the_cache_itself_is_never_pruned(project):
    write(project / "mutants" / "mutmut-cicd-stats.json", "{}")
    write(project / "mutants" / "pkg" / "__pycache__" / "gone.cpython-312.pyc", "")
    run()
    for kept in [
        "mutmut-stats.json",
        ".suite-hashes.json",
        "mutmut-cicd-stats.json",
        "pkg/sql.py.meta",
        "pkg/__pycache__/gone.cpython-312.pyc",
    ]:
        assert (project / "mutants" / kept).exists(), kept


def test_a_deleted_file_entry_leaves_the_tree(project):
    text = (project / "pyproject.toml").read_text()
    write(project / "pyproject.toml", text.replace("also_copy = [", 'also_copy = ["extra_conftest.py", '))
    write(project / "mutants" / "extra_conftest.py", "")
    write(project / "mutants" / "pyproject.toml", text)
    run()
    assert not (project / "mutants" / "extra_conftest.py").exists()
    assert (project / "mutants" / "pyproject.toml").exists()


def test_a_probe_script_is_not_part_of_the_suite(project):
    write(project / "tests" / "probe_thing.py", "print('investigating')\n")
    outcome, _ = run()
    assert outcome["changed"] == 0


def test_nothing_the_suite_loads_imports_a_probe():
    """What makes ignoring the probes safe. If a test or a helper starts
    importing one, it is part of the suite and has to be watched again."""
    importers = []
    for path in sorted((ROOT / "tests").rglob("*.py")) + sorted((ROOT / "benchmark").rglob("*.py")):
        if path.name.startswith("probe_"):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [alias.name for alias in node.names]
            if any(name.rpartition(".")[2].startswith("probe_") for name in names):
                importers.append(path.relative_to(ROOT).as_posix())
    assert importers == []


# ------------------------------------------------------- recorded killers
#
# tests/mutation_killers.py records the one test that failed under `-x`, which
# lets a kill rest on that test alone rather than on every test that reaches
# its function.


def write_killers(*records):
    lines = [json.dumps({"mutant": mutant, "killer": killer}) for mutant, killer in records]
    write(Path("mutants") / "mutmut-killers.jsonl", "\n".join(lines) + "\n")


def test_a_kill_survives_an_edit_to_a_test_that_did_not_kill_it(project):
    write_killers((SQL, "tests/sub/test_deep.py::test_deep"))
    write(project / "tests" / "test_sql.py", "def test_render(): pass\n")  # traced, not the killer
    run()
    assert verdict("sql", SQL) == 1


def test_a_kill_is_reset_when_its_killer_changes(project):
    write_killers((SQL, "tests/sub/test_deep.py::test_deep"))
    write(project / "tests" / "sub" / "test_deep.py", "def test_deep(): pass\n")
    run()
    assert verdict("sql", SQL) is None


def test_a_kill_is_reset_when_its_killer_is_deleted_even_if_the_map_lost_it(project):
    write_killers((CLI, "tests/test_gone.py::test_x"))
    write(project / "tests" / "test_gone.py", "")
    write(project / "mutants" / ".suite-hashes.json", json.dumps(cache.snapshot()))
    (project / "tests" / "test_gone.py").unlink()
    run()
    assert verdict("cli", CLI) is None


def test_the_latest_run_of_a_mutant_is_the_one_that_counts(project):
    """A later run with no failing test -- a pytest internal error -- leaves
    the kill unattributed, so it falls back to its traces."""
    write_killers((SQL, "tests/sub/test_deep.py::test_deep"), (SQL, None))
    write(project / "tests" / "test_sql.py", "def test_render(): pass\n")
    run()
    assert verdict("sql", SQL) is None


def test_a_killer_is_matched_under_the_name_mutmut_runs_it_by(project):
    # mutmut strips "__init__." before setting MUTANT_UNDER_TEST.
    name = "pkg.__init__.x_setup__mutmut_1"
    write_verdicts(cli={CLI: 1, name: 1})
    write_killers((CLI, "tests/test_cli.py::test_main"), ("pkg.x_setup__mutmut_1", "tests/test_cli.py::test_main"))
    write(project / "tests" / "test_sql.py", "def test_render(): pass\n")
    run()
    assert verdict("cli", name) == 1


def test_the_killer_record_is_compacted_to_one_line_per_mutant(project):
    write_killers((SQL, None), (SQL, "tests/test_sql.py::test_render"), (CLI, "tests/test_cli.py::test_main"))
    run()
    lines = (project / "mutants" / "mutmut-killers.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"mutant": CLI, "killer": "tests/test_cli.py::test_main"},
        {"mutant": SQL, "killer": "tests/test_sql.py::test_render"},
    ]


def test_a_torn_killer_line_is_ignored(project):
    write(project / "mutants" / "mutmut-killers.jsonl", json.dumps({"mutant": SQL, "killer": "x"}) + '\n{"mut')
    assert cache.read_killers(project / "mutants" / "mutmut-killers.jsonl") == {SQL: "x"}


# ------------------------------------------------------ per-test granularity

TWO_TESTS = """import pytest

LIMIT = 3


def helper():
    return LIMIT


def test_one():
    assert helper() == 3


class TestMore:
    @pytest.mark.parametrize("n", [1, 2])
    def test_n(self, n):
        assert n
"""


@pytest.fixture
def two_tests(project):
    write(project / "tests" / "test_sql.py", TWO_TESTS)
    write_stats({"pkg.sql.x_render": ["tests/test_sql.py::test_one", "tests/test_sql.py::TestMore::test_n[1]"]})
    write(project / "mutants" / ".suite-hashes.json", json.dumps(cache.snapshot()))
    return project


def test_editing_one_test_spares_a_kill_made_by_its_neighbour(two_tests):
    write_killers((SQL, "tests/test_sql.py::TestMore::test_n[1]"))
    write(two_tests / "tests" / "test_sql.py", TWO_TESTS.replace("assert helper() == 3", "assert helper()"))
    run()
    assert verdict("sql", SQL) == 1


def test_editing_a_test_class_stales_every_id_in_it(two_tests):
    write_killers((SQL, "tests/test_sql.py::TestMore::test_n[1]"))
    write(two_tests / "tests" / "test_sql.py", TWO_TESTS.replace("[1, 2]", "[1, 2, 3]"))
    run()
    assert verdict("sql", SQL) is None


def test_editing_the_rest_of_a_test_module_stales_every_id_in_it(two_tests):
    # Every test may read the constant, through a helper or a parametrize.
    write_killers((SQL, "tests/test_sql.py::TestMore::test_n[1]"))
    write(two_tests / "tests" / "test_sql.py", TWO_TESTS.replace("LIMIT = 3", "LIMIT = 4"))
    run()
    assert verdict("sql", SQL) is None


def test_a_comment_or_a_reformat_is_not_a_change(two_tests):
    edited = TWO_TESTS.replace("def test_one():", "# why this matters\ndef test_one():  # noqa\n").replace(
        "[1, 2]", "[1,2]"
    )
    write(two_tests / "tests" / "test_sql.py", edited)
    outcome, _ = run()
    assert outcome["changed"] == 0


def test_a_test_module_that_does_not_parse_stales_the_whole_file(two_tests):
    write_killers((SQL, "tests/test_sql.py::TestMore::test_n[1]"))
    write(two_tests / "tests" / "test_sql.py", TWO_TESTS + "\ndef broken(:\n")
    run()
    assert verdict("sql", SQL) is None


def test_a_new_test_in_an_existing_module_leaves_its_neighbours_kills(two_tests):
    write_killers((SQL, "tests/test_sql.py::test_one"))
    write(two_tests / "tests" / "test_sql.py", TWO_TESTS + "\n\ndef test_three():\n    pass\n")
    run()
    assert verdict("sql", SQL) == 1


# ------------------------------------------------------------ source changes
#
# mutmut resets a changed function's verdicts itself, but never re-traces for
# it: a test that now reaches new code keeps the trace it had.


def test_a_source_change_retraces_the_tests_that_reach_its_module(project):
    write(project / "pkg" / "sql.py", "def render(): ...\n\n\ndef helper(): ...\n")
    outcome, _ = run()
    stats = read_stats()
    assert outcome["moved"] == 1
    assert set(stats["duration_by_test"]) == {"tests/test_cli.py::test_main"}
    assert stats["tests_by_mangled_function_name"]["pkg.cli.x_main"] == ["tests/test_cli.py::test_main"]


def test_a_source_change_leaves_kills_to_mutmut(project):
    write(project / "pkg" / "sql.py", "def render(): return 1\n")
    run()
    assert verdict("sql", SQL) == 1
    assert verdict("cli", CLI) == 1


@pytest.mark.parametrize("code", [0, 33])
def test_a_source_change_resets_non_kills_anywhere(project, code):
    # A retraced test may now reach an unchanged function through a new call.
    write_verdicts(cli={CLI: code})
    write(project / "pkg" / "sql.py", "def render(): return 1\n")
    run()
    assert verdict("cli", CLI) is None


def test_a_source_change_is_not_a_suite_change_to_phase_two(project):
    write(project / "pkg" / "sql.py", "def render(): return 1\n")
    run()
    baseline = json.loads((project / "mutants" / ".suite-hashes.json").read_text())
    assert "pkg/sql.py" in baseline["sources"]
    assert "pkg/sql.py" not in baseline["suite"]


@pytest.mark.parametrize(
    "path, module",
    [
        ("pkg/sql.py", "pkg.sql"),
        ("pkg/models/__init__.py", "pkg.models"),
        ("pkg/models/fields.py", "pkg.models.fields"),
    ],
)
def test_module_of(path, module):
    assert cache.module_of(path) == module


def test_a_package_init_reaches_tests_under_either_spelling(project):
    stats = {
        "tests_by_mangled_function_name": {
            "pkg.models.__init__.x_a": ["tests/test_a.py::t"],
            "pkg.models.x_b": ["tests/test_b.py::t"],
            "pkg.modelsx.x_c": ["tests/test_c.py::t"],
        }
    }
    assert cache.tests_reaching({"pkg/models/__init__.py"}, stats) == {"tests/test_a.py::t", "tests/test_b.py::t"}


def test_an_old_flat_baseline_is_not_trusted(project):
    write(project / "mutants" / ".suite-hashes.json", json.dumps(cache.snapshot()["suite"]))
    run()
    assert verdict("sql", SQL) is None


def test_a_baseline_from_another_version_is_not_trusted(project):
    stale = cache.snapshot()
    stale["version"] = cache.BASELINE_VERSION - 1
    write(project / "mutants" / ".suite-hashes.json", json.dumps(stale))
    run()
    assert verdict("sql", SQL) is None
    assert verdict("cli", CLI) is None
