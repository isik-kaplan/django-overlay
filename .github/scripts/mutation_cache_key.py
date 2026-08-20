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

This lives in a file rather than inline in the workflow because it was inline
once, as a heredoc, and the closing delimiter was indented along with the YAML
around it. Bash then never terminated the heredoc, Python got `PY` as its last
line, and all six shards died before running a single mutant.
"""

import hashlib
import json
import sys
import tomllib
from pathlib import Path


def fingerprint(pyproject=Path("pyproject.toml"), lock=Path("uv.lock")):
    config = tomllib.loads(Path(pyproject).read_text())
    relevant = {
        "mutmut": config["tool"]["mutmut"],
        "dependencies": config["project"].get("dependencies", []),
        "lock": hashlib.sha256(Path(lock).read_bytes()).hexdigest(),
    }
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]


if __name__ == "__main__":
    print(fingerprint())
    sys.exit(0)
