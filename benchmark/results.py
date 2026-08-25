"""Saved runs, and the delta column that compares against them.

`--save-results` writes a run here. Every later run picks the most recent saved
run up automatically and adds a delta column, with no flag needed -- the flag
is for producing a baseline, not for using one.

Three rules keep the deltas honest:

  * a run taken in a different environment is not compared, it is reported as
    incomparable with the reason (see environment.py);
  * a change smaller than the noise floor is not shown at all. The harness
    cannot resolve a 5% move, and printing one invites somebody to go looking
    for a regression that is measurement error;
  * a run with a different set of query optimisations enabled *is* compared --
    that comparison is the whole point of the switches -- but the note says so,
    because a delta from a flag and a delta from a regression look identical.
"""

import json
import pathlib
from datetime import UTC, datetime

from benchmark import environment


DIRECTORY = pathlib.Path(__file__).parent / "results"

# Below this, a difference is the harness talking to itself. The ban probe's
# own control row -- a query neither column bans, run twice -- lands at 1.0x
# with excursions to about 1.2x, so anything under a fifth is not a signal.
NOISE_FLOOR = 0.20


def _slug(label):
    keep = [c if c.isalnum() or c in "-_" else "-" for c in label.lower()]
    return "".join(keep).strip("-") or "run"


def save(label, env, suites, lost=0):
    """Write a run. `lost` is how many cells were never measured at all.

    Kept rather than refused: the numbers that *were* measured are still worth
    reading, and throwing the file away would mean re-running to look at them.
    What it must not do is become a baseline by default -- see `latest()`.
    """
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    path = DIRECTORY / f"{_slug(label)}.json"
    path.write_text(
        json.dumps(
            {
                "label": label,
                "saved_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "environment": env,
                "lost": lost,
                "suites": suites,
            },
            indent=2,
        )
    )
    return path


def saved_runs():
    """Every saved run, newest first."""
    if not DIRECTORY.exists():
        return []
    found = []
    for path in DIRECTORY.glob("*.json"):
        try:
            found.append((path, json.loads(path.read_text())))
        except (OSError, json.JSONDecodeError):
            continue
    found.sort(key=lambda pair: pair[1].get("saved_at", ""), reverse=True)
    return found


def latest(label=None):
    """The newest saved run, or the one named.

    A run with cells that were never measured is skipped when picking
    automatically, and returned when named. The distinction is consent: a delta
    column nobody asked for must not be built on a run that half failed, and
    somebody who types `--compare-to` has said which run they mean.

    This is not hypothetical caution. A scale-1.0 run lost eleven cells to a
    compose database out of /dev/shm, saved itself under the default label, and
    the next run would have compared against it silently -- the numbers it did
    produce sitting next to `(was capped)` and blanks from queries that never
    ran at all.
    """
    runs = saved_runs()
    if label is not None:
        for path, data in runs:
            if data.get("label") == label or path.stem == _slug(label):
                return data
        return None
    for _, data in runs:
        if not data.get("lost"):
            return data
    return None


def clear():
    """Remove every saved run. Returns how many went."""
    if not DIRECTORY.exists():
        return 0
    removed = 0
    for path in DIRECTORY.glob("*.json"):
        path.unlink()
        removed += 1
    return removed


def _index(run):
    """(suite, section, row, column) -> cell dict, for a saved run."""
    table = {}
    for suite in run.get("suites", []):
        for section in suite.get("sections", []):
            for row in section.get("rows", []):
                for column, cell in row.get("cells", {}).items():
                    table[(suite["name"], section["title"], row["label"], column)] = cell
    return table


def deltas_for(baseline, suite_name, section):
    """{(row label, column): text} for one freshly measured section.

    Only cells the baseline also holds get a delta, so adding a row to a suite
    does not produce a column of blanks that reads as "unchanged".
    """
    if baseline is None:
        return {}
    previous = _index(baseline)
    out = {}
    for row in section.rows:
        for column, cell in row.cells.items():
            was = previous.get((suite_name, section.title, row.label, column))
            if was is None:
                continue
            text = _delta_text(was, cell)
            if text:
                out[(row.label, column)] = text
    return out


def _delta_text(was, now):
    """How to describe the move from a saved cell to a fresh one."""
    # Nothing to say about a cell that was never run on one side or the other.
    if was.get("note") or now.note:
        return ""
    # A cap boundary crossed either way is the whole story, and a percentage
    # across it would be invented -- a capped cell only says "at least".
    if was["capped"] and not now.capped:
        return "(was capped)"
    if now.capped and not was["capped"]:
        return "(now capped)"
    if was["capped"] and now.capped:
        return ""
    if not was["ms"]:
        return ""
    change = (now.ms - was["ms"]) / was["ms"]
    if abs(change) < NOISE_FLOOR:
        return ""
    return f"({change * 100:+.0f}%)"


def comparison_note(baseline, env):
    """One line about the baseline, or about why there isn't one.

    The runner decides whether to use the baseline by testing this string for
    its "delta" prefix, so the prefix is load-bearing: anything appended to a
    usable note has to be appended, not prepended.
    """
    if baseline is None:
        return None
    changed = environment.differences(baseline["environment"], env)
    if changed:
        return (
            f"no delta column: the baseline '{baseline['label']}' was taken in a "
            f"different environment ({'; '.join(changed)})"
        )
    note = f"delta column compares against '{baseline['label']}' from {baseline['saved_at']}"
    # A switch difference keeps the delta column -- that comparison is the
    # measurement, not a confound. But it gets named, because a column of
    # +21950% from an optimisation somebody turned off on purpose looks exactly
    # like a column of +21950% from a regression.
    switched = environment.switch_differences(baseline["environment"], env)
    if switched:
        note += f" -- and measures an optimisation change, not a code change: {'; '.join(switched)}"
    return note
