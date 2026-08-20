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
    return {
        str(path.relative_to(ROOT)) for path in PACKAGE.rglob("*.py")
    }


def assigned_files():
    return [path for paths in shards.SHARDS.values() for path in paths]


def test_every_module_belongs_to_exactly_one_shard():
    assigned = assigned_files()
    covered = set(assigned) | set(shards.NOT_SHARDED)
    missing = sorted(every_source_file() - covered)
    assert not missing, (
        "these modules are in no mutation shard, so no job mutates them: "
        + ", ".join(missing)
    )


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
        assert path in shards.NOT_SHARDED, (
            f"{path} is do_not_mutate in pyproject.toml but not exempt in the shard map"
        )
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
