"""The plugin that records which test killed each mutant during `mutmut run`."""

import json
from types import SimpleNamespace

import pytest

from tests import mutation_killers


@pytest.fixture
def in_mutants(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path / "mutmut-killers.jsonl"


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_mutant_run_records_its_start_and_its_killer(in_mutants, monkeypatch):
    monkeypatch.setenv("MUTANT_UNDER_TEST", "pkg.x_f__mutmut_1")
    mutation_killers.pytest_sessionstart(None)
    mutation_killers.pytest_runtest_logreport(SimpleNamespace(failed=False, nodeid="tests/test_a.py::test_ok"))
    mutation_killers.pytest_runtest_logreport(SimpleNamespace(failed=True, nodeid="tests/test_a.py::test_no"))
    assert records(in_mutants) == [
        {"mutant": "pkg.x_f__mutmut_1", "killer": None},
        {"mutant": "pkg.x_f__mutmut_1", "killer": "tests/test_a.py::test_no"},
    ]


@pytest.mark.parametrize("value", ["", "stats", "list_all_tests", "fail", "mutant_generation"])
def test_runs_that_are_not_a_mutant_record_nothing(in_mutants, monkeypatch, value):
    monkeypatch.setenv("MUTANT_UNDER_TEST", value)
    mutation_killers.pytest_sessionstart(None)
    mutation_killers.pytest_runtest_logreport(SimpleNamespace(failed=True, nodeid="tests/test_a.py::test_no"))
    assert not in_mutants.exists()


def test_a_plain_pytest_run_records_nothing(in_mutants, monkeypatch):
    monkeypatch.delenv("MUTANT_UNDER_TEST", raising=False)
    mutation_killers.pytest_runtest_logreport(SimpleNamespace(failed=True, nodeid="tests/test_a.py::test_no"))
    assert not in_mutants.exists()


def test_the_plugin_writes_where_the_step_reads():
    from tests.test_invalidate_mutation_cache import cache

    assert mutation_killers.KILLERS.name == cache.KILLERS.name
