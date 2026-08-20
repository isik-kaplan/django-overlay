"""The fingerprint the mutation cache is keyed on.

A dependency or mutmut-config change has to discard every cached verdict:
mutmut's change detection is git-based and knows nothing about dependencies, so
reusing results across a Django upgrade would report mutants as settled when
nothing had re-verified them.

But that means *what* goes into the key, exactly. It used to be
hashFiles('uv.lock', 'pyproject.toml'), which threw away four hours of results
over three edited comment lines -- pyproject.toml also carries the ruff config,
the coverage settings and the version number, none of which can change a
mutation result. So the key comes from the parsed [tool.mutmut] table, the
declared dependencies, and a hash of uv.lock: TOML parsing drops comments and
formatting, and sorting the keys makes it insensitive to reordering.

The shard's own file list goes in too, and has to. `only_mutate` is written
into pyproject.toml *after* this runs, so it is not in the parsed table above --
and it was left out on the reasoning that varying which files are mutated needs
no result invalidation, since "new mutants are born uncached and dropped ones
simply stop being walked". True of results, false of the tree they live in:
mutmut builds `mutants/` once and never adds scaffolding for a file a later
`only_mutate` newly includes. So a shard whose file list changed would restore
a tree built for the old list, mutate nothing, and report the old file's
verdicts as this run's. That is not hypothetical -- splitting models.py into a
package changed what the `models` shard owns, and without this the next run
would have reported ~736 kills for a file that no longer exists.

Each shard already has its own key namespace, so including the list only
invalidates a shard when that shard's own files change, which is exactly when
invalidation is needed. check_mutants.tree_problems() is the second layer, for
if this one ever fails.

This lives in a file rather than inline in the workflow because it was inline
once, as a heredoc, and the closing delimiter was indented along with the YAML
around it. Bash then never terminated the heredoc, Python got `PY` as its last
line, and all six shards died before running a single mutant.
"""

import hashlib
import importlib.util
import json
import sys
import tomllib
from pathlib import Path


def shard_files(shard):
    """The files `shard` owns, straight from the map CI selects with."""
    if shard is None:
        return []
    path = Path(__file__).resolve().parent / "mutation_shards.py"
    spec = importlib.util.spec_from_file_location("mutation_shards", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if shard not in module.SHARDS:
        raise SystemExit(f"unknown shard {shard!r}; known: {', '.join(sorted(module.SHARDS))}")
    return module.SHARDS[shard]


def fingerprint(pyproject=Path("pyproject.toml"), lock=Path("uv.lock"), shard=None):
    config = tomllib.loads(Path(pyproject).read_text())
    mutmut = {key: value for key, value in config["tool"]["mutmut"].items() if key != "only_mutate"}
    relevant = {
        # only_mutate is dropped and `shard` supplies that dimension instead, so
        # the key does not depend on whether a shard happened to be selected
        # when it was computed. It used to: the workflow computes this before
        # selecting, and a local run computing it afterwards got a different
        # answer for the same shard.
        "mutmut": mutmut,
        "dependencies": config["project"].get("dependencies", []),
        "lock": hashlib.sha256(Path(lock).read_bytes()).hexdigest(),
        "shard_files": shard_files(shard),
    }
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]


if __name__ == "__main__":
    print(fingerprint(shard=sys.argv[1] if len(sys.argv) > 1 else None))
    sys.exit(0)
