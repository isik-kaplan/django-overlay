"""What the machine was, so two runs can be told apart.

A benchmark number without its environment is not a measurement, it is an
anecdote. Postgres 16 and 17 plan the appendrel differently; `work_mem` decides
whether a hash join spills; `shared_buffers` decides whether the second round
of a best-of-three reads from memory. Every one of those has changed a headline
number in this project by more than the effects being measured.

So each saved run carries its environment, and `comparable()` refuses to put a
delta column against a run that was taken somewhere else. Silently comparing a
3M/PG17 run against a 1M/PG16 one is worse than offering no comparison at all:
it dresses noise up as a regression.
"""

import os
import platform
import subprocess

from django.db import connection

from benchmark import switches


# The settings that have actually moved a number in this project. Anything here
# differing between two runs makes them incomparable.
PG_SETTINGS = (
    "work_mem",
    "shared_buffers",
    "effective_cache_size",
    "random_page_cost",
    "max_parallel_workers_per_gather",
    "jit",
)

# The subset that must match for a delta column to mean anything. `cap_ms` is
# in here because a capped cell renders as ">10s" or ">30s" depending on it,
# and those are not the same claim.
COMPARABILITY_KEYS = (
    "postgres_major",
    "work_mem",
    "shared_buffers",
    "max_parallel_workers_per_gather",
    "scale",
    "share",
    "cap_ms",
    "cores",
)


def _git_sha():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git
        return "unknown"
    return result.stdout.strip() or "unknown"


def capture(scale, share, cap_ms, passes):
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        version = cursor.fetchone()[0]
        settings = {}
        for name in PG_SETTINGS:
            cursor.execute(f"SHOW {name}")
            settings[name] = cursor.fetchone()[0]

    return {
        "postgres_version": version,
        "postgres_major": version.split(".")[0],
        **settings,
        "cores": os.cpu_count(),
        "platform": f"{platform.system()} {platform.machine()}",
        "scale": scale,
        "share": share,
        "cap_ms": cap_ms,
        "passes": passes,
        "git_sha": _git_sha(),
        # Which of the four library optimisations the run had on. Recorded from
        # the settings object, so this says what the library obeyed rather than
        # what the CLI asked for.
        "switches": switches.configured(),
    }


def switch_differences(left, right):
    """Which optimisations were on in one run and off in the other.

    Deliberately not in COMPARABILITY_KEYS -- the one thing recorded here that
    is left out of it. Every key in that tuple invalidates a comparison: a
    different work_mem makes two numbers unrelated, so no delta beats a wrong
    one. The switches are the opposite. Turning one off and comparing *is* the
    measurement, so suppressing the delta column would suppress the result.
    What it must not do is happen quietly, which is what this is for.
    """
    before, after = left.get("switches") or {}, right.get("switches") or {}
    flags = {switches.option_name(s): s.flag for s in switches.SWITCHES}
    changed = []
    for name in sorted(set(before) | set(after)):
        # Absent means on, here as everywhere else: a run saved before the
        # switches existed must not report four differences against a fresh one.
        was, now = bool(before.get(name, True)), bool(after.get(name, True))
        if was != now:
            changed.append(f"{flags.get(name, name)} {'on' if was else 'off'} -> {'on' if now else 'off'}")
    return changed


def differences(left, right):
    """Which comparability keys disagree between two environments."""
    changed = []
    for key in COMPARABILITY_KEYS:
        if str(left.get(key)) != str(right.get(key)):
            changed.append(f"{key} {left.get(key)} -> {right.get(key)}")
    return changed


def comparable(left, right):
    return not differences(left, right)


def summarise(environment):
    line = (
        f"postgres {environment['postgres_version']} on {environment['cores']} cores, "
        f"work_mem {environment['work_mem']}, shared_buffers {environment['shared_buffers']}, "
        f"scale {environment['scale']}, share {environment['share']}, "
        f"cap {environment['cap_ms'] // 1000}s, {environment['passes']} pass(es)"
    )
    off = switches.describe(environment.get("switches") or {})
    if off:
        line += f"; optimisations OFF: {', '.join(off)}"
    return line
