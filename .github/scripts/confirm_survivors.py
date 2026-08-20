"""Phase two: re-run every mutant that survived against the whole suite.

Phase one (`mutmut run`) tests each mutant against only the tests mutmut's
tracing associates with it. That is cheap -- a couple of seconds rather than a
full suite pass -- and it is wrong in exactly one direction. Tracing can miss a
test that would have killed the mutant; it cannot invent one that kills a mutant
the suite would have let live. So a kill in phase one is final, and everything
tracing gets wrong arrives here, in the survivor pile.

This script settles that pile: for each survivor it runs the entire suite inside
mutants/ with that mutant active, which is the same thing a human does by hand
to check a reported survivor. Exit non-zero means some test did object after
all, and the mutant is killed. Exit zero means it really does survive: a change
to django_overlay/ that the whole suite is fine with.

Two modes, mirroring check_mutants.py:

    confirm_survivors.py --label ddl          # one shard, after `mutmut run`
    confirm_survivors.py --aggregate shards/  # the union, in the report job

Only survivors cost a full pass, which is what makes a full mutation run fit
inside a CI job at all -- see the note on pytest_add_cli_args in pyproject.toml.
"""

import argparse
import collections
import glob
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


REPORT = Path("mutants/mutmut-confirmed.json")
# Lives inside mutants/ so the workflow's existing cache carries it between
# runs, the same way phase one's verdicts travel.
CACHE = Path("mutants/mutmut-confirmed-cache.json")
CACHE_VERSION = 1

# Exit codes mutmut treats as a kill. Anything else left the mutant alive in
# phase one and therefore needs settling here. Kept in step with
# mutmut.__main__.status_by_exit_code.
KILLED_CODES = (1, 3, -24)
# ...except this one, which is not a verdict at all.
SKIPPED_CODE = 34

# Long enough to be no limit for a suite that takes ~100s, short enough that a
# mutant which deadlocks costs one slot rather than the whole job.
DEFAULT_TIMEOUT = 900


def alive_from_meta(root="mutants"):
    """(alive, unchecked) mutant names out of the .meta files mutmut writes.

    Read from the meta files rather than from export-cicd-stats, because the
    stats are counts and this needs names.
    """
    alive, unchecked = [], []
    for path in sorted(glob.glob(f"{root}/**/*.meta", recursive=True)):
        try:
            codes = json.loads(Path(path).read_text()).get("exit_code_by_key", {})
        except (OSError, json.JSONDecodeError):
            continue
        for name, code in codes.items():
            if code is None:
                unchecked.append(name)
            elif code not in KILLED_CODES and code != SKIPPED_CODE:
                alive.append(name)
    return sorted(alive), sorted(unchecked)


def run_full_suite(name, timeout=DEFAULT_TIMEOUT, root="mutants"):
    """(exit_code, seconds) for the whole suite with one mutant active.

    MUTANT_UNDER_TEST is how mutmut's trampoline decides which body to call, so
    setting it and running pytest normally is exactly what phase one does --
    minus the test selection, which is the whole point.
    """
    started = time.monotonic()
    try:
        finished = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-x", "-q", "--tb=no",
             "-p", "no:randomly", "-p", "no:random-order", "--rootdir=."],
            cwd=root,
            env={**os.environ, "MUTANT_UNDER_TEST": name},
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # Nothing killed it and it would not stop, which is not a pass.
        return None, time.monotonic() - started, None
    return finished.returncode, time.monotonic() - started, killing_test(finished.stdout)


def killing_test(output):
    """The node id of the test that failed, out of pytest's short summary.

    `-x` means there is at most one, and knowing which it was is what lets a
    kill be cached: the verdict holds for as long as that test does.
    """
    for line in reversed((output or "").splitlines()):
        if line.startswith("FAILED "):
            return line.split(None, 1)[1].split(" ")[0]
    return None


def sha(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def function_hashes(root="mutants"):
    """mutant name prefix -> the hash mutmut keeps of the function it mutates.

    mutmut already tracks this to decide which mutants to re-run in phase one,
    and reusing it means phase two invalidates on exactly the same source
    changes rather than on a rule of its own.
    """
    hashes = {}
    for path in glob.glob(f"{root}/**/*.meta", recursive=True):
        try:
            meta = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for name, value in meta.get("hash_by_function_name", {}).items():
            hashes[name] = value
    return hashes


def function_hash_for(mutant_name, hashes):
    """django_overlay.sql.x_render__mutmut_3 -> the hash of x_render."""
    mangled = mutant_name.partition("__mutmut_")[0]
    return hashes.get(mangled.rpartition(".")[2])


def suite_hash(root="mutants"):
    """One hash over every test file, which is what a survivor verdict rests on.

    "Nothing kills this mutant" is a claim about the whole suite, so the whole
    suite is what invalidates it. Coarser than the per-test pinning a kill gets,
    and necessarily so: there is no single test to point at, because the
    verdict is that none of them objected.
    """
    digest = hashlib.sha256()
    for path in sorted(glob.glob(f"{root}/tests/**/*.py", recursive=True)):
        digest.update(path.encode())
        digest.update(Path(path).read_bytes())
    return digest.hexdigest()[:16]


def read_cache(path=CACHE):
    try:
        cached = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if cached.get("version") != CACHE_VERSION:
        return {}
    return cached.get("verdicts", {})


def reusable(entry, mutant_name, hashes, root="mutants"):
    """Whether a cached verdict still answers the question being asked.

    Every verdict needs the mutated function unchanged. What else it needs
    depends on what the verdict claimed, and the two are not symmetrical.

    A *kill* is "this test objected", so it is pinned to that one test: stored
    with the node id, reusable while the file holding it is unchanged. Letting
    it go stale would report a mutant as dead by a test that no longer exists,
    which is the false green this whole workstream keeps producing.

    A *survivor* is "nothing objected", which is a claim about the entire
    suite, so the entire suite is what invalidates it. Coarser on purpose --
    there is no single test to point at. It used to be never reused at all, on
    the theory that survivors are few; the first real shard came back 18 out of
    18 confirmed, which makes them the bulk of phase two rather than the tail
    of it, and re-running every one of them on a push that changed no test is
    work with a known answer.
    """
    if not entry:
        return False
    if entry.get("function_hash") != function_hash_for(mutant_name, hashes):
        return False
    if not entry.get("killed_by"):
        return entry.get("suite_hash") == suite_hash(root)
    test_file = entry["killed_by"].partition("::")[0]
    return entry.get("test_file_hash") == sha(Path(root) / test_file)


def baseline_is_clean(run=run_full_suite, say=print):
    """Whether the suite passes inside mutants/ with no mutant active.

    Without this, phase two has the false-green hole its own predecessor had.
    A verdict here is "the suite failed, so the mutant is dead", and a suite
    that fails for its own reasons -- a collection error, a missing fixture, a
    stale copy of something in mutants/ -- fails for every mutant equally. That
    reads as every survivor being killed, and the build goes green having
    confirmed nothing. One pass up front is the cheapest way to know the
    verdicts mean what they say.
    """
    code, seconds, _ = run("")
    if code == 0:
        say(f"baseline: the suite passes inside mutants/ ({seconds:.0f}s)")
        return True
    say(f"baseline: the suite does NOT pass inside mutants/ with no mutant active "
        f"(exit {code} after {seconds:.0f}s).\nEvery confirmation would report a kill it "
        "did not measure, so nothing here can be trusted.")
    return False


Outcome = collections.namedtuple("Outcome", "confirmed killed hung reused verdicts")


def confirm(names, run=run_full_suite, say=print, cache=None, hashes=None, root="mutants"):
    """Settle each survivor, reusing the kills that are still answerable."""
    cache = read_cache() if cache is None else cache
    hashes = function_hashes(root) if hashes is None else hashes
    confirmed, killed, hung, reused = [], [], [], []
    verdicts = {}
    for index, name in enumerate(names, start=1):
        where = f"[{index}/{len(names)}]"
        entry = cache.get(name)
        if reusable(entry, name, hashes, root):
            reused.append(name)
            verdicts[name] = entry
            # Which list it goes in is the whole verdict. A reused entry that
            # landed in `killed` regardless would turn every cached survivor
            # into a pass, which is the failure this cache exists next to.
            if entry["killed_by"]:
                killed.append(name)
                say(f"{where} cached    {name} (killed by {entry['killed_by']})")
            else:
                confirmed.append(name)
                say(f"{where} cached    {name} (survives; no test has changed)")
            continue

        code, seconds, killed_by = run(name)
        if code is None:
            hung.append(name)
            say(f"{where} HUNG      {name} (no verdict in the time allowed)")
        elif code == 0:
            confirmed.append(name)
            say(f"{where} SURVIVED  {name} ({seconds:.0f}s)")
            verdicts[name] = {
                "killed_by": None,
                "function_hash": function_hash_for(name, hashes),
                "suite_hash": suite_hash(root),
            }
        else:
            killed.append(name)
            say(f"{where} killed    {name} ({seconds:.0f}s, by {killed_by or 'unknown'})")
            # A kill nobody can attribute is not cached: the whole guarantee is
            # that the verdict outlives only the test that produced it.
            if killed_by:
                verdicts[name] = {
                    "killed_by": killed_by,
                    "function_hash": function_hash_for(name, hashes),
                    "test_file_hash": sha(Path(root) / killed_by.partition("::")[0]),
                }
    return Outcome(confirmed, killed, hung, reused, verdicts)


def write_report(path, label, confirmed, killed, hung, unchecked):
    payload = {
        "label": label,
        "confirmed": confirmed,
        "killed_by_full_suite": killed,
        "hung": hung,
        "unchecked": unchecked,
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def find_reports(directory):
    """(label, payload) for every shard's report under a downloaded artifact tree."""
    found = []
    for path in sorted(Path(directory).rglob("mutmut-confirmed.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            found.append((path.parent.name, {"unreadable": str(error)}))
            continue
        found.append((payload.get("label") or path.parent.name, payload))
    return found


def render(rows):
    """One line per shard, then the names that matter."""
    lines = [f"{'shard':<12} {'survived':>8} {'killed here':>12} {'hung':>6} {'unchecked':>10}"]
    lines.append("-" * len(lines[0]))
    for label, payload in rows:
        if "unreadable" in payload:
            lines.append(f"{label:<12} unreadable: {payload['unreadable']}")
            continue
        lines.append(
            f"{label:<12} {len(payload.get('confirmed', [])):>8}"
            f" {len(payload.get('killed_by_full_suite', [])):>12}"
            f" {len(payload.get('hung', [])):>6} {len(payload.get('unchecked', [])):>10}"
        )
    names = [name for _, payload in rows for name in payload.get("confirmed", [])]
    hung = [name for _, payload in rows for name in payload.get("hung", [])]
    if names:
        lines += ["", f"{len(names)} surviving mutant(s):"]
        lines += [f"  {name}" for name in names[:50]]
        if len(names) > 50:
            lines.append(f"  ... and {len(names) - 50} more")
    if hung:
        lines += ["", f"{len(hung)} mutant(s) with no verdict:"]
        lines += [f"  {name}" for name in hung[:50]]
    return "\n".join(lines)


def expected_shards():
    """The shard names, read from the map next door so the two cannot drift."""
    import importlib.util

    path = Path(__file__).resolve().parent / "mutation_shards.py"
    spec = importlib.util.spec_from_file_location("mutation_shards", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module.SHARDS)


def emit(rendered, summary, heading):
    print(rendered)
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"\n## {heading}\n\n```\n{rendered}\n```\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="local", help="the shard being confirmed")
    parser.add_argument("--aggregate", default=None,
                        help="a directory of downloaded per-shard reports; union mode")
    parser.add_argument("--expect-every-shard", action="store_true",
                        help="in union mode, fail unless every shard reported")
    parser.add_argument("--summary", default=None, help="append the table here")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="seconds one confirmation may take")
    parser.add_argument("--names", nargs="*", default=None,
                        help="confirm these mutants instead of the survivors on disk")
    options = parser.parse_args(argv)

    if options.aggregate:
        rows = find_reports(options.aggregate)
        problems = []
        if options.expect_every_shard:
            absent = sorted(expected_shards() - {label for label, _ in rows})
            if absent:
                problems.append("no phase-two report from shard(s): " + ", ".join(absent)
                                + " -- the union is incomplete, so it cannot be a pass")
        emit(render(rows) if rows else "(no reports)", options.summary, "Mutation — survivors")
        alive = sum(len(payload.get("confirmed", [])) + len(payload.get("hung", []))
                    for _, payload in rows)
        unreadable = [label for label, payload in rows if "unreadable" in payload]
        if unreadable:
            problems.append("unreadable report(s) from: " + ", ".join(unreadable))
        if problems:
            print("\n" + "\n".join(problems))
            return 1
        if alive:
            print(f"\n{alive} mutant(s) survive the whole suite. Either add a test that kills "
                  "one,\nor -- if it is genuinely equivalent -- put `# pragma: no mutate` on the "
                  "line\nwith a comment saying why.")
            return 1
        print("\nno surviving mutants")
        return 0

    names, unchecked = (options.names, []) if options.names else alive_from_meta()
    runner = lambda name: run_full_suite(name, timeout=options.timeout)  # noqa: E731
    if not names:
        print(f"{options.label}: nothing survived phase one, so there is nothing to confirm")
    else:
        print(f"{options.label}: confirming {len(names)} survivor(s) against the whole suite")
        if not baseline_is_clean(run=runner):
            return 1
    outcome = confirm(names, run=runner)
    payload = write_report(REPORT, options.label, outcome.confirmed, outcome.killed,
                           outcome.hung, unchecked)
    CACHE.write_text(json.dumps(
        {"version": CACHE_VERSION, "verdicts": outcome.verdicts}, indent=2, sort_keys=True
    ) + "\n")
    if outcome.reused:
        print(f"{len(outcome.reused)} of {len(names)} verdict(s) came from the cache, "
              "each still pinned to the test that produced it")
    emit(render([(options.label, payload)]), options.summary, f"Mutation — {options.label}")

    if unchecked:
        print(f"\n{len(unchecked)} mutant(s) have no phase-one verdict at all -- `mutmut run` "
              "did not\nfinish. Nothing here can be trusted until it does.")
        return 1
    if outcome.confirmed or outcome.hung:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
