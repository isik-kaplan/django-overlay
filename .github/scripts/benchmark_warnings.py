"""Turn a benchmark log into annotations and a closing summary.

GitHub has no `warning` job status -- a job passes or it fails -- so anything
worth noticing has to be said in two places to actually get read: as a
`::warning::` annotation, which lands at the top of the run page, and again as
plain text at the very end of the log, where somebody who scrolled to the
bottom will see it.

The three things worth saying:

  * the budget. What was estimated, what it cost, what the ceiling was. This
    is the line that explains a short run, and it is printed loudly whether or
    not anything went wrong.
  * suites skipped for budget. A run that quietly covered less than it looks
    like it did is the failure mode the guard exists to prevent.
  * rows where the overlay and the plain mirror disagreed. The full suite does
    not gate on these -- the smoke job already did -- but they must not pass
    unremarked.
  * connections the harness had to throw away. A lost connection costs a
    measurement, and the cell says so, but the reason is only ever on stderr.
"""

import pathlib
import re
import sys


def annotate(level, message):
    print(f"::{level}::{message}")


def budget_block(lines):
    """The last BUDGET banner in the log -- the one written after the run."""
    found = []
    for index, line in enumerate(lines):
        if line.strip() != "BUDGET":
            continue
        block = []
        for following in lines[index + 1 :]:
            if following.startswith("="):
                break
            if following.strip():
                block.append(following.strip())
        found = block
    return found


def main(path):
    source = pathlib.Path(path)
    text = source.read_text(errors="replace") if source.exists() else ""
    if not text.strip():
        annotate("warning", "the benchmark produced no log -- it probably failed to start")
        return 0

    lines = [line.rstrip() for line in text.splitlines()]
    budget = budget_block(lines)
    skipped = [line.strip() for line in lines if line.lstrip().startswith("SKIPPED ")]
    disagreements = [line.strip() for line in lines if re.match(r"^\s+- .+: ", line)]
    lost = [line.strip() for line in lines if line.lstrip().startswith("LOST CONNECTION ")]

    if skipped:
        annotate(
            "warning",
            "benchmark suites were skipped for budget: " + "; ".join(item.replace("SKIPPED ", "") for item in skipped),
        )
    if disagreements:
        annotate(
            "warning", f"{len(disagreements)} row(s) where the overlay and the plain mirror returned different answers"
        )
    if lost:
        annotate(
            "warning",
            f"the harness lost its connection {len(lost)} time(s) -- those cells are not measurements: {lost[0]}",
        )
    if budget:
        annotate("notice", "benchmark budget -- " + "; ".join(budget))

    print()
    print("=" * 78)
    print("  BENCHMARK SUMMARY")
    for line in budget or ["no budget block found in the log"]:
        print(f"  {line}")
    if skipped:
        print("  --")
        for item in skipped:
            print(f"  {item}")
    if disagreements:
        print("  --")
        print(f"  {len(disagreements)} disagreement(s) between overlay and plain:")
        for item in disagreements[:20]:
            print(f"  {item}")
    if lost:
        print("  --")
        print(f"  {len(lost)} lost connection(s):")
        for item in lost[:20]:
            print(f"  {item}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "benchmark.log"))
