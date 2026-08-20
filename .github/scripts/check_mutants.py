"""Fail the build unless every mutant died.

`mutmut run` exits 0 whether or not mutants survived, so the policy has to be
enforced separately. Reads mutants/mutmut-cicd-stats.json, written by
`mutmut export-cicd-stats`.

A surviving mutant means some change to django_overlay/ that no test objects
to. `suspicious` and `timeout` count as alive too: an unstable or hanging
mutant is one nothing reliably kills. `no_tests` means mutmut found no test
covering the code at all, which is the same problem stated more bluntly.
`skipped` is the one benign bucket — those are the `# pragma: no mutate`
lines, each of which should carry a comment saying why it's equivalent.
"""

import json
import sys
from pathlib import Path


STATS = Path("mutants/mutmut-cicd-stats.json")

# status key -> whether it violates the policy
ALIVE = ("survived", "suspicious", "timeout", "no_tests", "segfault")


def main() -> int:
    if not STATS.exists():
        print(f"{STATS} not found — did `mutmut run` and `mutmut export-cicd-stats` both run?")
        return 1

    stats = json.loads(STATS.read_text())
    alive = {key: stats.get(key, 0) for key in ALIVE}
    total_alive = sum(alive.values())

    print(f"{'total':<12}{stats.get('total', 0)}")
    print(f"{'killed':<12}{stats.get('killed', 0)}")
    print(f"{'skipped':<12}{stats.get('skipped', 0)}  (# pragma: no mutate)")
    for key, count in alive.items():
        print(f"{key:<12}{count}")

    if stats.get("check_was_interrupted_by_user"):
        print("\nrun was interrupted — results are incomplete, refusing to pass")
        return 1

    if total_alive:
        print(
            f"\n{total_alive} mutant(s) still alive. Run `uv run mutmut browse` to see them.\n"
            "Either add a test that kills it, or — if the mutant is genuinely equivalent —\n"
            "put `# pragma: no mutate` on the line with a comment saying why."
        )
        return 1

    print("\nno surviving mutants")
    return 0


if __name__ == "__main__":
    sys.exit(main())
