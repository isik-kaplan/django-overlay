"""Records which test killed each mutant, during `mutmut run` and nowhere else.

mutmut keeps an exit code per mutant and nothing about which test produced it.
That leaves invalidate_mutation_cache.py one way to decide whether an edited
test has staled a cached kill: any test traced to the mutated function changed.
For a function nearly every test reaches, that is every edit -- measured, one
edited test module reset 232 of the `query` shard's 254 verdicts.

But mutmut runs each mutant's tests with `-x`, so exactly one test fails, and
that test is the whole of what the kill rests on. Recording it lets a kill be
pinned the way confirm_survivors.py already pins its own: reusable while that
one test is unchanged.

Loaded through pytest_add_cli_args in [tool.mutmut], so plain pytest runs and
phase two never see it. It acts only when MUTANT_UNDER_TEST names a mutant --
mutmut sets it to "stats", "list_all_tests", "fail" and the like for runs that
are not one. Each run first appends a record with no killer, so the last record
for a mutant always describes its latest run: a kill with no failing test
behind it (a pytest internal error) is left unattributed rather than credited
to whatever killed it last time.
"""

import json
import os
from pathlib import Path


# Relative to mutants/, which is where mutmut runs pytest.
KILLERS = Path("mutmut-killers.jsonl")


def _mutant():
    name = os.environ.get("MUTANT_UNDER_TEST", "")
    return name if "__mutmut_" in name else None


def _record(mutant, killer):
    with KILLERS.open("a") as f:
        f.write(json.dumps({"mutant": mutant, "killer": killer}) + "\n")


def pytest_sessionstart(session):
    mutant = _mutant()
    if mutant:
        _record(mutant, None)


def pytest_runtest_logreport(report):
    # Any phase: a fixture the mutant breaks fails in setup, and that test is
    # still what killed it.
    mutant = _mutant()
    if mutant and report.failed:
        _record(mutant, report.nodeid)
