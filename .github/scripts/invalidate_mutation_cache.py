"""Throw out the cached verdicts a test edit has made stale, before `mutmut run`.

mutmut's incremental cache is nearly complete. It re-runs a mutant whose
function changed, re-traces a test it has not seen, and drops a deleted test
from its map. What it cannot see is a test *edited in place*: per-function
hashes cover only the files it mutates, and its git change detection drops
every `.py` file on the theory that those hashes already cover them -- true of
django_overlay/, false of tests/. So a mutant killed by a test that has since
been weakened keeps its kill, and phase one never re-checks a kill (only
survivors go on to confirm_survivors.py). A restored cache would then report a
mutant as dead by a test that no longer kills it. That is a false green, the
exact failure this whole pipeline is built to refuse.

So this runs between restoring `mutants/` and running mutmut. It compares every
file the suite is made of against a baseline stored beside the cache, and for
what changed:

  - resets the kills that rested on a changed test, so they are tested again;
  - resets every verdict that was not a kill, because a new or edited test can
    kill a mutant whose cached "survived" or "no tests" mutmut would otherwise
    reuse forever -- mutmut re-traces a new test but never re-runs a mutant it
    already has an answer for;
  - drops the changed tests from mutmut's stats map, so its own new-test path
    re-traces exactly them;
  - deletes files under mutants/ whose source is gone, because mutmut copies
    over the tree but never deletes from it, and pytest runs from inside it --
    a deleted test file would otherwise still be collected.

How a changed file maps to tests is the part that is easy to get wrong:

  - a test module affects its own test ids;
  - a conftest.py affects every test id under its directory;
  - anything else -- a helper module, the test app's models, the settings, the
    SQL templates, benchmark/, which tests import -- affects tests nobody can
    name from here, so it resets everything.

Two copies of this logic exist, here and in openfield, by agreement: a shared
module would force the two repos onto one mutmut version.
"""

import argparse
import ast
import fnmatch
import hashlib
import json
import sys
import tomllib
from pathlib import Path


MUTANTS = Path("mutants")
STATS = MUTANTS / "mutmut-stats.json"
# Written by tests/mutation_killers.py while mutmut runs.
KILLERS = MUTANTS / "mutmut-killers.jsonl"
# Its own file, not a key in the stats map: mutmut's load_stats() ends with
# `assert not data`, so any key it does not know crashes the whole run.
BASELINE = MUTANTS / ".suite-hashes.json"
PYPROJECT = Path("pyproject.toml")
# Bump whenever what the baseline's keys mean changes -- a new unit of hashing,
# a file newly watched or ignored. A baseline from another version is not
# diffed against this one but thrown out, which resets everything once: a diff
# across two key shapes would be right only by accident.
BASELINE_VERSION = 1

# Every changed file affects these, rather than a set of ids.
EVERYTHING = object()

# Copied into mutants/ but not watched. The only tests that read .github/ are
# the ones testing the mutation tooling itself -- test_mutation_shards.py and
# test_confirm_survivors.py -- and they never call into django_overlay/, so
# they cannot kill a mutant there and their edits cannot stale a verdict.
# Watching it would reset every shard whenever the workflow changed.
UNWATCHED = (".github",)

# Files inside a watched root that the suite never loads. The probes are
# standalone investigation scripts: pytest's default python_files does not
# collect them, and nothing the suite imports imports them --
# tests/test_invalidate_mutation_cache.py fails if that stops being true. They
# are also among the most edited files in tests/, and as helpers they would
# reset every verdict in every shard each time.
UNLOADED = ("tests/probe_*.py",)

# The exit codes mutmut records as a kill. Kept in step with confirm_survivors.py,
# which explains why -24 is not one of them.
KILLED_CODES = (1, 3)
# "Caught by type check". mutmut re-derives it every run from a fresh type
# check, and resetting it here would be worse than redundant: its mutant loop
# records "no tests" before it looks at the type checker, so a reset 37 on an
# untraced function would come back in a different category.
TYPE_CHECKED = 37

# mutmut writes these beside each mutated module. They mirror a source file
# without having its name.
SIDECARS = (".meta", ".spans")


def mutmut_config(pyproject=PYPROJECT):
    return tomllib.loads(Path(pyproject).read_text())["tool"]["mutmut"]


def watched_roots(pyproject=PYPROJECT):
    """The directories the suite runs out of, from also_copy.

    also_copy is already the list of what the tests need inside mutants/, so it
    is the list of what can change their outcome. pyproject.toml itself is
    handled by pyproject_hash() instead of being watched whole.
    """
    roots = []
    for entry in mutmut_config(pyproject).get("also_copy", []):
        path = Path(entry)
        if path.name == PYPROJECT.name or path.parts[0] in UNWATCHED:
            continue
        roots.append(path)
    return roots


def pyproject_hash(pyproject=PYPROJECT):
    """A hash of the part of pyproject.toml that changes how tests run.

    Not the whole file: CI rewrites [tool.mutmut] on every run to select the
    shard, and [tool.mutmut] is in the cache key anyway. The pytest table is
    what is left -- an addopts or a marker change alters every test without
    touching one.
    """
    table = tomllib.loads(Path(pyproject).read_text()).get("tool", {}).get("pytest", {})
    return hashlib.sha256(json.dumps(table, sort_keys=True).encode()).hexdigest()[:16]


def hash_suite(roots, pyproject=PYPROJECT):
    """path -> content hash for every file in `roots`, plus the pytest config."""
    hashes = {}
    for root in roots:
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for path in files:
            if "__pycache__" in path.parts or any(fnmatch.fnmatch(path.as_posix(), p) for p in UNLOADED):
                continue
            if is_test_module(path.as_posix()) and path.suffix == ".py":
                hashes.update(hash_test_module(path))
            else:
                hashes[path.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    hashes[f"{PYPROJECT.name}[tool.pytest]"] = pyproject_hash(pyproject)
    return hashes


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def hash_test_module(path):
    """One hash per test in a module, and one for everything else in it.

    So that editing one test stales that test's ids rather than the whole
    file's. The units are what pytest names first in a node id: a top-level
    test_* function, or a whole Test* class. The rest of the module -- imports,
    helpers, fixtures, the constants a parametrize reads -- is one more unit,
    keyed by the bare path, and a change there stales every id in the file.

    Hashed from the AST, so a comment or a reformat changes nothing. A module
    that does not parse is hashed whole, under the bare path, which stales the
    file -- pytest will be failing on it anyway.
    """
    source = path.read_bytes()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {path.as_posix(): hashlib.sha256(source).hexdigest()[:16]}
    hashes = {}
    rest = []
    for node in tree.body:
        if is_test_unit(node):
            hashes[f"{path.as_posix()}::{node.name}"] = _digest(ast.dump(node))
        else:
            rest.append(node)
    hashes[path.as_posix()] = _digest(ast.dump(ast.Module(body=rest, type_ignores=[])))
    return hashes


def is_test_unit(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name.startswith("test")
    return isinstance(node, ast.ClassDef) and node.name.startswith("Test")


def read_baseline(path=BASELINE):
    """The stored snapshot(), or None if there is none this version can read."""
    try:
        baseline = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(baseline, dict) or baseline.get("version") != BASELINE_VERSION:
        return None
    if not all(isinstance(baseline.get(k), dict) for k in ("suite", "sources")):
        return None
    return baseline


def changed_files(current, baseline):
    """Paths added, edited or deleted since the baseline was written."""
    return {path for path in current.keys() | baseline.keys() if current.get(path) != baseline.get(path)}


def is_test_module(path):
    """pytest's default python_files, which pyproject.toml does not override.

    Takes a baseline key as well as a path: tests/test_x.py::test_y is a unit
    of tests/test_x.py.
    """
    path = path.partition("::")[0]
    name = Path(path).name
    return Path(path).parts[0] == "tests" and (name.startswith("test_") or name.endswith("_test.py"))


def affected_tests(changed, known_ids):
    """The test ids whose verdicts `changed` could have moved, or EVERYTHING."""
    affected = set()
    for path in changed:
        if is_test_module(path):
            module, _, unit = path.partition("::")
            affected |= {test for test in known_ids if file_of(test) == module and (not unit or unit_of(test) == unit)}
        elif Path(path).name == "conftest.py":
            directory = Path(path).parent.as_posix() + "/"
            affected |= {test for test in known_ids if file_of(test).startswith(directory)}
        else:
            return EVERYTHING
    return affected


def unit_of(test_id):
    """tests/test_sql.py::TestX::test_y[a] -> TestX"""
    return test_id.partition("::")[2].partition("::")[0].partition("[")[0]


def file_of(test_id):
    """tests/test_sql.py::TestX::test_y[a] -> tests/test_sql.py"""
    return test_id.removeprefix("mutants/").partition("::")[0]


def read_stats(path=STATS):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def known_test_ids(stats):
    ids = set(stats.get("duration_by_test", {}))
    for tests in stats.get("tests_by_mangled_function_name", {}).values():
        ids |= set(tests)
    return ids


def run_name(mutant_name):
    """The name mutmut runs a mutant under, which is what MUTANT_UNDER_TEST carries."""
    return mutant_name.replace("__init__.", "")


def mangled_name(mutant_name):
    """The stats map's key for a mutant, the way mutmut itself looks it up."""
    return run_name(mutant_name).partition("__mutmut_")[0]


def read_killers(path=KILLERS):
    """mutant -> the test that failed on its latest run, or None if none did.

    The last record wins: tests/mutation_killers.py appends one with no killer
    at the start of every run, so an older run's killer never outlives a newer
    run of the same mutant.
    """
    killers = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return killers
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a run cut off mid-write
        killers[record["mutant"]] = record["killer"]
    return killers


def write_killers(killers, path=KILLERS):
    """Rewrite the record with one line per mutant, so it does not grow forever."""
    Path(path).write_text("".join(json.dumps({"mutant": m, "killer": k}) + "\n" for m, k in sorted(killers.items())))


def invalidate_verdicts_for(affected, stats, root=MUTANTS, killers=None):
    """Reset verdicts in every .meta: kills resting on `affected`, and every non-kill.

    A kill whose killing test was recorded stays while that test is unchanged:
    under `-x` it is the one test the verdict rests on. One with no recorded
    killer -- from before the recording existed, or a pytest internal error --
    falls back to the coarse rule, and stays only if none of the tests traced
    to its function changed. Traces only ever grow -- load_stats() merges with
    |= -- so the test that produced the kill is still in that set.

    Everything that was not a kill is reset whenever anything changed at all,
    because the edit or addition may be exactly the test that kills it, and
    mutmut reuses a cached survivor or no-tests verdict without asking. Those
    re-runs are cheap: phase one runs only traced tests, and a mutant with no
    traced tests is recorded again without running anything.

    Safe to run before mutmut rebuilds the tree, although the rebuild re-keys
    verdicts: this only ever writes None, and the rebuild keeps an unchanged
    function's verdict as it finds it and sets every other one to None. Make
    this write anything else and that stops holding.
    """
    traces = stats.get("tests_by_mangled_function_name", {}) if stats else {}
    killers = {} if killers is None else killers
    count = 0
    for meta_path in sorted(Path(root).rglob("*.meta")):
        meta = json.loads(meta_path.read_text())
        codes = meta.get("exit_code_by_key", {})
        dirty = False
        for name, code in codes.items():
            if code is None or code == TYPE_CHECKED:
                continue
            if affected is EVERYTHING or code not in KILLED_CODES:
                stale = True
            elif killers.get(run_name(name)):
                stale = killers[run_name(name)] in affected
            else:
                stale = not affected.isdisjoint(traces.get(mangled_name(name), ()))
            if stale:
                codes[name] = None
                dirty = True
                count += 1
        if dirty:
            meta_path.write_text(json.dumps(meta, indent=4))
    return count


def prune_stats_map(affected, path=STATS):
    """Drop `affected` from the map, so mutmut's new_tests() re-traces exactly them.

    From both halves of it. duration_by_test alone is what new_tests() reads,
    so pruning only that re-traces the test -- but the re-trace merges into the
    old edges with |=, so the edges the test no longer has would survive it. A
    prune that looks like it worked and does not. clear_out_obsolete_test_names()
    in mutmut does the same two removals for deleted tests.

    watched_file_hashes and git_commit are left alone: they are mutmut's own
    change-detection baseline, and only a full run refreshes them.
    """
    stats = read_stats(path)
    if stats is None:
        return 0
    durations = stats.get("duration_by_test", {})
    traces = stats.get("tests_by_mangled_function_name", {})
    if affected is EVERYTHING:
        removed = len(durations)
        stats["duration_by_test"] = {}
        stats["tests_by_mangled_function_name"] = {}
    else:
        removed = 0
        for test in list(durations):
            if test in affected:
                del durations[test]
                removed += 1
        for name, tests in traces.items():
            traces[name] = [test for test in tests if test not in affected]
    Path(path).write_text(json.dumps(stats))
    return removed


def prune_orphaned_tree(mirrored, root=MUTANTS):
    """Delete files under mutants/ that mirror a path in `mirrored` whose source is gone.

    Only mirrors. mutants/ also holds the stats map, the .meta and .spans files,
    __pycache__ and this script's own baseline -- the cache this exists to keep
    -- so anything that is not a copy of a source path is left alone. A sidecar
    goes with the module it describes.
    """
    removed = 0
    for source_root in mirrored:
        tree = Path(root) / source_root
        if tree.is_file():
            # A file entry mutmut stopped copying when it disappeared, say a
            # conftest.py, which pytest would otherwise go on loading.
            if not Path(source_root).exists():
                tree.unlink()
                removed += 1
            continue
        if not tree.is_dir():
            continue
        for path in sorted(tree.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            source = path.relative_to(root)
            for suffix in SIDECARS:
                if source.name.endswith(suffix):
                    source = source.with_name(source.name.removesuffix(suffix))
                    break
            if not source.exists():
                path.unlink()
                removed += 1
    return removed


def hash_sources(pyproject=PYPROJECT):
    """path -> content hash for every module mutmut mutates from."""
    hashes = {}
    for root in mutmut_config(pyproject).get("source_paths", []):
        for path in sorted(Path(root).rglob("*.py")):
            if "__pycache__" not in path.parts:
                hashes[path.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return hashes


def snapshot(pyproject=PYPROJECT):
    """What the baseline records: the suite, and the source apart from it.

    Apart, because the two mean different things. A suite change says which
    verdicts to distrust; a source change only says which traces are out of
    date, since mutmut resets a changed function's verdicts itself.
    confirm_survivors.py reads the suite half alone for the same reason.
    """
    return {
        "version": BASELINE_VERSION,
        "suite": hash_suite(watched_roots(pyproject), pyproject),
        "sources": hash_sources(pyproject),
    }


def module_of(path):
    """django_overlay/models/fields.py -> django_overlay.models.fields"""
    dotted = path.removesuffix(".py").replace("/", ".")
    return dotted.removesuffix(".__init__")


def tests_reaching(moved, stats):
    """The tests the map says reach any module in `moved`.

    mutmut re-traces a test only when it is new, never because the code it
    reaches changed. So a test that now reaches a function it did not -- a new
    function, or an old one behind a new call -- never gains that edge, and the
    function's mutants run against too few tests, or none, which records them
    as "no tests". Dropping the tests that already reach the module makes
    mutmut trace them again and find whatever the module gained. What this
    cannot find is a test that never reached the module before the change; that
    test is almost always new or edited itself, which retraces it anyway.
    """
    prefixes = tuple(module_of(path) + "." for path in moved)
    reaching = set()
    for name, tests in (stats or {}).get("tests_by_mangled_function_name", {}).items():
        if run_name(name).startswith(prefixes) or name.startswith(prefixes):
            reaching |= set(tests)
    return reaching


def invalidate(pyproject=PYPROJECT, root=MUTANTS, say=print):
    """The whole pre-run step. Returns what it did, for the log and for tests."""
    root = Path(root)
    current = snapshot(pyproject)
    baseline_path = root / BASELINE.name
    stats_path = root / STATS.name
    killers_path = root / KILLERS.name

    if not root.is_dir():
        # Nothing restored, so the run about to start builds the whole tree
        # against the suite as it is now. Writing the baseline before it does
        # is what lets the next run trust that tree.
        root.mkdir()
        baseline_path.write_text(json.dumps(current, indent=1, sort_keys=True))
        say("No restored mutants/ -- nothing cached to invalidate; baseline written.")
        return {"changed": 0, "moved": 0, "verdicts": 0, "tests": 0, "orphans": 0}

    stats = read_stats(stats_path)
    killers = read_killers(killers_path)
    baseline = read_baseline(baseline_path)
    if baseline is None:
        # A tree with no baseline came from before this step existed. Nothing
        # says what suite it was built against, so nothing in it is trusted.
        changed = set(current["suite"])
        moved = set()
        affected = EVERYTHING
        say("No suite baseline in the restored tree; resetting every verdict.")
    else:
        changed = changed_files(current["suite"], baseline["suite"])
        moved = changed_files(current["sources"], baseline["sources"])
        if not changed:
            affected = set()
        elif stats is None:
            # Verdicts with no traces to say which tests they rest on.
            affected = EVERYTHING
        else:
            # The recorded killers too: a test the map has lost is still the
            # one a kill rests on, and a deleted file has to reach it.
            affected = affected_tests(changed, known_test_ids(stats) | set(filter(None, killers.values())))

    verdicts = tests = 0
    if changed or moved:
        # A source change resets no kill -- mutmut resets a changed function's
        # verdicts against its own hashes -- but it does reset every non-kill,
        # which may be a mutant a retraced test now reaches.
        verdicts = invalidate_verdicts_for(affected, stats, root, killers)
        retrace = affected if affected is EVERYTHING else affected | tests_reaching(moved, stats)
        tests = prune_stats_map(retrace, stats_path)
    config = mutmut_config(pyproject)
    orphans = prune_orphaned_tree([Path(p) for p in config.get("source_paths", []) + config.get("also_copy", [])], root)

    if killers:
        write_killers(killers, killers_path)

    # Last, so a step that dies part-way leaves the old baseline behind, and
    # the next run invalidates again rather than trusting a half-done reset.
    baseline_path.write_text(json.dumps(current, indent=1, sort_keys=True))

    scope = "everything" if affected is EVERYTHING else f"{len(affected)} test id(s)"
    say(
        f"{len(changed)} suite file(s) changed, affecting {scope}; {len(moved)} source module(s) changed: "
        f"reset {verdicts} verdict(s), dropped {tests} test(s) from the stats map, deleted {orphans} orphaned file(s)."
    )
    for path in sorted(changed | moved)[:50]:
        say(f"  changed: {path}")
    return {"changed": len(changed), "moved": len(moved), "verdicts": verdicts, "tests": tests, "orphans": orphans}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    invalidate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
