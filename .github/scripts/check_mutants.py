"""Fail the build unless every mutant died.

`mutmut run` exits 0 whether or not mutants survived, so the policy has to be
enforced separately. Reads the JSON written by `mutmut export-cicd-stats`.

A full pass is six sharded jobs, so this runs twice over: once inside each
shard, against that shard's file, so a red X points at the subsystem; and once
at the end over every shard's uploaded stats, because the policy applies to the
union. Given a directory it reads every mutmut-cicd-stats.json underneath it.

This is phase one of two, so `--phase-one` reports the counts without failing
on them: tracing under-selects, and confirm_survivors.py re-runs each survivor
against the whole suite to find out which are real. Without the flag every
alive mutant fails the build, which is what the union check at the end wants.

A surviving mutant means some change to django_overlay/ that no test objects
to. `suspicious` and `timeout` count as alive too: an unstable or hanging
mutant is one nothing reliably kills. `no_tests` means mutmut found no test
covering the code at all, which is the same problem stated more bluntly.
`skipped` is the one benign bucket — those are the `# pragma: no mutate`
lines, each of which should carry a comment saying why it's equivalent.
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path


DEFAULT_STATS = Path("mutants/mutmut-cicd-stats.json")

# status key -> whether it violates the policy
ALIVE = ("survived", "suspicious", "timeout", "no_tests", "segfault")

BUCKETS = ("total", "killed", "skipped", *ALIVE)


def find_stats(paths):
    """(label, data) per stats file, in a stable order.

    A directory is the aggregate case: actions/download-artifact lays each
    shard's file out under its artifact name, so the directory name is the
    shard name and makes the label without having to be told it.
    """
    found = []
    for path in paths:
        path = Path(path)
        if path.is_dir():
            for stats in sorted(path.rglob("mutmut-cicd-stats.json")):
                label = stats.parent.name.replace("mutmut-stats-", "")
                found.append((label, stats))
        else:
            found.append((path.parent.name, path))
    return found


def expected_shards():
    """Every shard name from the map next door.

    Read from mutation_shards.py rather than passed in, so the set this
    demands can never drift from the set CI runs.
    """
    path = Path(__file__).resolve().parent / "mutation_shards.py"
    spec = importlib.util.spec_from_file_location("mutation_shards", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module.SHARDS)


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return {"unreadable": str(error)}


def alive_in(stats):
    return sum(stats.get(key, 0) for key in ALIVE)


def unrun(stats):
    """Whether a run produced mutants but a verdict for none of them.

    "No surviving mutants" and "no mutants ran" produce identical numbers in
    every bucket, and only one of them is good news. Observed for real: the
    test suite failed to collect inside mutants/, so `mutmut run` exited before
    testing anything and left total=2209 with every bucket at zero — which an
    earlier version of this script called a pass.
    """
    accounted = stats.get("killed", 0) + stats.get("skipped", 0) + alive_in(stats)
    return bool(stats.get("total", 0)) and not accounted


def table(rows):
    """One line per shard plus a total. Widest column wins; no dependencies."""
    columns = ["shard", *BUCKETS]
    widths = {name: len(name) for name in columns}
    for label, stats in rows:
        widths["shard"] = max(widths["shard"], len(label))
        for name in BUCKETS:
            widths[name] = max(widths[name], len(f"{stats.get(name, 0):,}"))

    lines = ["  ".join(name.rjust(widths[name]) for name in columns)]
    lines[0] = lines[0].replace("shard".rjust(widths["shard"]), "shard".ljust(widths["shard"]), 1)
    lines.append("-" * len(lines[0]))
    for label, stats in rows:
        cells = [label.ljust(widths["shard"])]
        cells += [f"{stats.get(name, 0):,}".rjust(widths[name]) for name in BUCKETS]
        lines.append("  ".join(cells))
    if len(rows) > 1:
        totals = {name: sum(stats.get(name, 0) for _, stats in rows) for name in BUCKETS}
        lines.append("-" * len(lines[0]))
        cells = ["TOTAL".ljust(widths["shard"])]
        cells += [f"{totals[name]:,}".rjust(widths[name]) for name in BUCKETS]
        lines.append("  ".join(cells))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=None,
                        help="stats files, or directories to search for them")
    parser.add_argument("--label", default=None,
                        help="name for a single file's row (the shard being run)")
    parser.add_argument("--summary", default=None,
                        help="also append the table here (GITHUB_STEP_SUMMARY)")
    parser.add_argument("--expect-every-shard", action="store_true",
                        help="fail unless every shard in mutation_shards.py reported")
    parser.add_argument("--phase-one", action="store_true",
                        help="do not fail on mutants left alive; confirm_survivors.py settles "
                             "those. The false-green guards still apply.")
    options = parser.parse_args(argv)

    found = find_stats(options.paths or [DEFAULT_STATS])
    if not found:
        print(f"no mutmut-cicd-stats.json found under {options.paths or [DEFAULT_STATS]} — "
              "did `mutmut run` and `mutmut export-cicd-stats` both run?")
        return 1

    rows, problems = [], []
    for label, path in found:
        if not path.exists():
            problems.append(f"{path} not found — did `mutmut export-cicd-stats` run?")
            continue
        stats = read(path)
        if "unreadable" in stats:
            problems.append(f"{path} could not be read: {stats['unreadable']}")
            continue
        rows.append((options.label or label or path.stem, stats))

    for label, stats in rows:
        if stats.get("check_was_interrupted_by_user"):
            problems.append(f"{label}: run was interrupted — results are incomplete")
        if unrun(stats):
            problems.append(
                f"{label}: none of the {stats.get('total', 0):,} mutants have a result — "
                "`mutmut run` did not test anything. Check that the suite collects "
                "inside mutants/ before trusting these numbers."
            )

    # A shard that dies before uploading leaves no artifact, and five green
    # shards plus one missing one would otherwise read as "no surviving
    # mutants". The policy is over the union, so a hole in the union is a
    # failure, not an abstention.
    if options.expect_every_shard:
        absent = sorted(expected_shards() - {label for label, _ in rows})
        if absent:
            problems.append(
                "no results from shard(s): " + ", ".join(absent)
                + " -- the union is incomplete, so it cannot be a pass"
            )

    rendered = table(rows) if rows else "(no results)"
    print(rendered)
    if options.summary:
        with open(options.summary, "a", encoding="utf-8") as handle:
            handle.write(f"\n## Mutation\n\n```\n{rendered}\n```\n")

    total_alive = sum(alive_in(stats) for _, stats in rows)
    if problems:
        print("\n" + "\n".join(problems))
        return 1
    # Phase one under-selects tests on purpose, so mutants left alive here are
    # candidates, not verdicts -- see confirm_survivors.py. What still has to
    # hold is that the run happened at all, which is checked above.
    if options.phase_one:
        print(f"\n{total_alive:,} mutant(s) left for phase two to settle")
        return 0
    if total_alive:
        print(
            f"\n{total_alive:,} mutant(s) still alive. Run `uv run mutmut browse` to see them —\n"
            "scope it to one shard with `python .github/scripts/mutation_shards.py <shard>`.\n"
            "Either add a test that kills it, or — if the mutant is genuinely equivalent —\n"
            "put `# pragma: no mutate` on the line with a comment saying why."
        )
        return 1

    print("\nno surviving mutants")
    return 0


if __name__ == "__main__":
    sys.exit(main())
