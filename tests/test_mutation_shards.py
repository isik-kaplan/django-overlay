"""The mutation shard map has to cover the package exactly once.

A full mutation pass is six CI jobs, each mutating a different slice of
django_overlay/ while running the whole test suite. The slices are a
hand-written map in .github/scripts/mutation_shards.py, and a hand-written map
of files is the kind of thing that goes stale the first time somebody adds a
module: the new file belongs to no shard, no job mutates it, and every report
still says every mutant died. Nothing would ever point at it.

So the map is asserted against the filesystem here, in the ordinary test run
rather than only in the mutation job -- which is hours long and gated to
maintainers. Add a module without assigning it and the plain `pytest` run fails
with the name of the file.
"""

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "django_overlay"


def load_shards():
    """Import the script by path -- .github/scripts/ is not an importable package."""
    path = ROOT / ".github" / "scripts" / "mutation_shards.py"
    spec = importlib.util.spec_from_file_location("mutation_shards", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("mutation_shards", module)
    spec.loader.exec_module(module)
    return module


shards = load_shards()


def every_source_file():
    return {str(path.relative_to(ROOT)) for path in PACKAGE.rglob("*.py")}


def assigned_files():
    return [path for paths in shards.SHARDS.values() for path in paths]


def test_every_module_belongs_to_exactly_one_shard():
    assigned = assigned_files()
    covered = set(assigned) | set(shards.NOT_SHARDED)
    missing = sorted(every_source_file() - covered)
    assert not missing, "these modules are in no mutation shard, so no job mutates them: " + ", ".join(missing)


def test_no_module_is_mutated_by_two_shards():
    """Two shards mutating one file is duplicated hours, not extra safety."""
    assigned = assigned_files()
    duplicated = sorted({path for path in assigned if assigned.count(path) > 1})
    assert not duplicated, f"assigned to more than one shard: {duplicated}"


def test_no_shard_names_a_file_that_is_gone():
    """A deleted module leaves a pattern that silently matches nothing."""
    stale = sorted(path for path in assigned_files() if not (ROOT / path).exists())
    assert not stale, f"named by a shard but not on disk: {stale}"


def test_nothing_is_both_sharded_and_exempt():
    overlap = sorted(set(assigned_files()) & set(shards.NOT_SHARDED))
    assert not overlap, f"both assigned and exempt: {overlap}"


def test_every_exemption_gives_a_reason():
    """The exempt list is the one that can grow by accident, so it costs prose."""
    for path, reason in shards.NOT_SHARDED.items():
        assert reason.strip(), f"{path} is exempt with no reason given"


def test_the_excluded_module_is_the_one_pyproject_excludes():
    """do_not_mutate and the exempt list are two statements of one fact."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["mutmut"]
    for path in config.get("do_not_mutate", []):
        assert path in shards.NOT_SHARDED, f"{path} is do_not_mutate in pyproject.toml but not exempt in the shard map"
        assert path not in assigned_files(), f"{path} is do_not_mutate but assigned to a shard"


def test_selecting_a_shard_rewrites_only_the_mutmut_section(tmp_path):
    original = (ROOT / "pyproject.toml").read_text()
    scratch = tmp_path / "pyproject.toml"
    scratch.write_text(original)

    shards.select("ddl", scratch)
    config = tomllib.loads(scratch.read_text())

    assert config["tool"]["mutmut"]["only_mutate"] == shards.SHARDS["ddl"]
    # The rest of the file has to survive intact: mutmut reads also_copy and the
    # timeout settings from the same section, and the build reads the rest of it.
    assert config["tool"]["mutmut"]["timeout_constant"] == 900.0
    assert ".github/" in config["tool"]["mutmut"]["also_copy"]
    assert config["project"]["dependencies"] == ["django>=6.0", "jinja2>=3.1.0"]
    assert config["tool"]["coverage"]["report"]["fail_under"] == 100


def test_selecting_twice_leaves_one_block(tmp_path):
    """CI reruns a step; a script that appends each time corrupts the file."""
    scratch = tmp_path / "pyproject.toml"
    scratch.write_text((ROOT / "pyproject.toml").read_text())

    shards.select("models", scratch)
    shards.select("cli", scratch)
    shards.select("cli", scratch)

    assert scratch.read_text().count(shards.MARKER) == 1
    config = tomllib.loads(scratch.read_text())
    assert config["tool"]["mutmut"]["only_mutate"] == shards.SHARDS["cli"]


def test_an_unknown_shard_is_refused(tmp_path):
    scratch = tmp_path / "pyproject.toml"
    scratch.write_text((ROOT / "pyproject.toml").read_text())
    with pytest.raises(SystemExit):
        shards.select("nonexistent", scratch)


def test_the_shard_names_are_what_the_workflow_dispatches():
    """The matrix in .github/workflows/mutation.yml is the same list."""
    workflow = (ROOT / ".github" / "workflows" / "mutation.yml").read_text()
    for shard in shards.SHARDS:
        assert shard in workflow, f"shard {shard!r} has no job in mutation.yml"


# ------------------------------------ the unreachable probe's documented size
#
# The probe covers what mutmut structurally cannot. It ran as one CI job per
# region while it was the longest job in the mutation workflow; it is one job
# again now that it is not, so there is no matrix left to keep in sync. What
# still rots is the count written into the prose.


def probe_mutants():
    """The unreachable-mutant probe's own list, imported by path."""
    path = ROOT / "tests" / "probe_unreachable_mutants.py"
    spec = importlib.util.spec_from_file_location("probe_unreachable_mutants", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MUTANTS


# docs/ is not in mutmut's also_copy, so it does not exist inside mutants/ and
# the suite that runs there cannot read it. Skipped rather than copied: adding a
# path to also_copy changes the [tool.mutmut] table, which is in the mutation
# cache-key fingerprint, so it would discard every shard's phase-one tree --
# hours of settled verdicts -- to let a documentation assertion run somewhere it
# has nothing to say. Nothing under docs/ is mutated, and the ordinary test run
# covers it. ROOT.name is the detector because the copied tree is literally the
# directory called "mutants"; a missing-file check would skip silently forever
# if the docs were ever moved.
@pytest.mark.skipif(
    ROOT.name == "mutants",
    reason="docs/ is not copied into the mutant tree, and nothing in it is mutated",
)
def test_the_documented_mutation_count_is_the_real_one():
    """The prose in DEVELOPMENT.md carries a number, and numbers in prose rot.

    It said "~30 hand-chosen mutations" while the list held 43. Nothing was
    wrong with the harness -- the count simply stopped being true and no reader
    could tell. A count worth writing down is worth asserting.
    """
    docs = (ROOT / "docs" / "development" / "DEVELOPMENT.md").read_text()
    total = len(probe_mutants())
    assert f"{total} hand-chosen mutations" in docs, (
        f"DEVELOPMENT.md does not say there are {total} hand-chosen mutations; "
        "probe_unreachable_mutants.py has grown or shrunk since it was written"
    )


# ---------------------------------------------- the tree belongs to the code
#
# mutmut builds mutants/ once and does not add scaffolding for a file that a
# later only_mutate newly includes. Three shards run in sequence therefore
# reported the same 325 mutants and 321 kills, because only the first had ever
# been mutated -- and nothing in the totals could show it. The same staleness
# reaches CI through a restored cache when a shard's file list changes.


def load_check_mutants():
    path = ROOT / ".github" / "scripts" / "check_mutants.py"
    spec = importlib.util.spec_from_file_location("check_mutants", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_mutants", module)
    spec.loader.exec_module(module)
    return module


def a_tree(tmp_path, *, meta_for, only_mutate=None, real=()):
    """A mutants/ tree with .meta files for `meta_for`, and a pyproject whose
    only_mutate is `only_mutate`. `real` are the files that exist on disk."""
    tree = tmp_path / "mutants"
    for name in meta_for:
        target = tree / f"{name}.meta"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
    for name in real:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")
    pyproject = tmp_path / "pyproject.toml"
    scope = (
        "" if only_mutate is None else "only_mutate = [\n" + "".join(f'    "{path}",\n' for path in only_mutate) + "]\n"
    )
    pyproject.write_text("[tool.mutmut]\n" + scope)
    return tree, pyproject


def test_a_tree_matching_its_shard_is_accepted(tmp_path, monkeypatch):
    check = load_check_mutants()
    tree, pyproject = a_tree(tmp_path, meta_for=["pkg/a.py"], only_mutate=["pkg/a.py"], real=["pkg/a.py"])
    monkeypatch.chdir(tmp_path)
    assert check.tree_problems(tree, pyproject) == []


def test_a_file_the_shard_claims_but_never_mutated_is_refused(tmp_path, monkeypatch):
    """The half that made three shards report one shard's numbers."""
    check = load_check_mutants()
    tree, pyproject = a_tree(
        tmp_path,
        meta_for=["pkg/a.py"],
        only_mutate=["pkg/b.py"],
        real=["pkg/a.py", "pkg/b.py"],
    )
    monkeypatch.chdir(tmp_path)
    problems = check.tree_problems(tree, pyproject)
    assert any("pkg/b.py" in problem and "no results" in problem for problem in problems)
    assert any("pkg/a.py" in problem and "does not own" in problem for problem in problems)


def test_results_for_a_deleted_file_are_refused(tmp_path, monkeypatch):
    """What a restored cache looks like after a module is split or renamed: the
    tree still answers for the file that used to be there."""
    check = load_check_mutants()
    tree, pyproject = a_tree(tmp_path, meta_for=["pkg/gone.py"], only_mutate=None)
    monkeypatch.chdir(tmp_path)
    problems = check.tree_problems(tree, pyproject)
    assert any("no longer exist" in problem and "pkg/gone.py" in problem for problem in problems)


def test_an_absent_tree_is_not_a_problem(tmp_path, monkeypatch):
    """The union check at the end of a run reads uploaded stats, with no tree."""
    check = load_check_mutants()
    _, pyproject = a_tree(tmp_path, meta_for=[], only_mutate=None)
    monkeypatch.chdir(tmp_path)
    assert check.tree_problems(tmp_path / "nonexistent", pyproject) == []


# ------------------------------------------------------ the cache key's scope


def load_cache_key():
    path = ROOT / ".github" / "scripts" / "mutation_cache_key.py"
    spec = importlib.util.spec_from_file_location("mutation_cache_key", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("mutation_cache_key", module)
    spec.loader.exec_module(module)
    return module


def test_each_shard_gets_its_own_cache_key():
    """Not for isolation -- the key is already prefixed by shard name -- but so
    that changing a shard's file list invalidates that shard's tree."""
    key = load_cache_key()
    keys = {shard: key.fingerprint(shard=shard) for shard in shards.SHARDS}
    assert len(set(keys.values())) == len(keys), f"two shards share a key: {keys}"


def test_the_key_does_not_depend_on_whether_a_shard_is_selected(tmp_path):
    """CI computes the key before selecting; a person debugging computes it
    after. Those have to agree, or the local key names a cache CI never wrote."""
    key = load_cache_key()
    unselected = tmp_path / "pyproject.toml"
    unselected.write_text((ROOT / "pyproject.toml").read_text())
    selected = tmp_path / "selected.toml"
    selected.write_text((ROOT / "pyproject.toml").read_text())
    shards.select("models", selected)

    lock = ROOT / "uv.lock"
    assert key.fingerprint(unselected, lock, shard="models") == key.fingerprint(selected, lock, shard="models")


def test_losing_a_module_invalidates_every_shard(monkeypatch):
    """A module deleted from one shard makes every *other* shard's tree stale
    too, because `mutants/` holds a copy of the whole package and the suite
    runs inside it. Splitting swaps/ six ways is how this was found: the six
    new shards started cold and passed, and all eight untouched ones restored
    a tree still holding rows.py and shape.py, where this very file failed the
    baseline on modules that belong to no shard.

    So the key covers the package's whole module list, not just the shard's
    slice -- and an untouched shard's key has to move when a module anywhere
    disappears."""
    key = load_cache_key()
    before = {shard: key.fingerprint(shard=shard) for shard in shards.SHARDS}

    surviving = dict(shards.SHARDS)
    victim = next(s for s, paths in surviving.items() if len(paths) > 1)
    surviving[victim] = surviving[victim][:-1]
    monkeypatch.setattr(shards, "SHARDS", surviving)
    monkeypatch.setattr(key, "_shard_map", lambda: shards)

    after = {shard: key.fingerprint(shard=shard) for shard in surviving}
    unchanged = sorted(shard for shard in after if after[shard] == before[shard])
    assert not unchanged, f"these shards would restore a tree holding the deleted module: {unchanged}"


def test_an_unknown_shard_has_no_key():
    key = load_cache_key()
    with pytest.raises(SystemExit):
        key.fingerprint(shard="nonexistent")
