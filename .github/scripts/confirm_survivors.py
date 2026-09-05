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
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path


REPORT = Path("mutants/mutmut-confirmed.json")
# Lives inside mutants/ so the workflow's existing cache carries it between
# runs, the same way phase one's verdicts travel.
CACHE = Path("mutants/mutmut-confirmed-cache.json")
# Bump this whenever a change here alters how a verdict is reached or what it is
# pinned to. Nothing else will: the workflow's cache key covers the mutmut
# config and the lockfile, and deliberately not this file -- hashing a file that
# is mostly commentary would discard a whole shard's phase-one tree over an
# edited docstring, which is the trade mutation_cache_key.py exists to refuse.
# So the verdicts this file wrote before the change survive the change, and the
# only thing that can throw them out is this number. Bumping it costs one phase
# two; not bumping it keeps the wrong verdicts the change was made to stop.
CACHE_VERSION = 2
EQUIVALENTS = Path(__file__).resolve().parent.parent / "mutation-equivalents.toml"
# Long enough that "equivalent" or "n/a" does not pass for an explanation.
SHORTEST_USEFUL_REASON = 40

# Exit codes mutmut treats as a kill. Anything else left the mutant alive in
# phase one and therefore needs settling here.
#
# Read mutmut.__main__.status_by_exit_code carefully before touching this. That
# dict literal lists `-24: "killed"` near the top and `-24: "timeout"` fifteen
# lines further down; the later key wins, so -24 is a timeout. It was in here as
# a kill, which meant a mutant that hung until the CPU limit shot it was counted
# as dead and never confirmed. The models shard had exactly one.
KILLED_CODES = (1, 3)
# ...except this one, which is not a verdict at all.
SKIPPED_CODE = 34

# Long enough to be no limit for a suite that takes ~100s, short enough that a
# mutant which deadlocks costs one slot rather than the whole job.
DEFAULT_TIMEOUT = 900


def alive_from_meta(root="mutants"):
    """(alive, unchecked, total) out of the .meta files mutmut writes.

    Read from the meta files rather than from export-cicd-stats, because the
    stats are counts and this needs names. `total` is returned so the caller can
    tell "every mutant died" from "there were no mutants", which produce
    identical survivor lists and mean opposite things.
    """
    alive, unchecked, total = [], [], 0
    for path in sorted(glob.glob(f"{root}/**/*.meta", recursive=True)):
        try:
            codes = json.loads(Path(path).read_text()).get("exit_code_by_key", {})
        except (OSError, json.JSONDecodeError):
            continue
        for name, code in codes.items():
            total += 1
            if code is None:
                unchecked.append(name)
            elif code not in KILLED_CODES and code != SKIPPED_CODE:
                alive.append(name)
    return sorted(alive), sorted(unchecked), total


def run_full_suite(name, timeout=DEFAULT_TIMEOUT, root="mutants"):
    """(exit_code, seconds) for the whole suite with one mutant active.

    MUTANT_UNDER_TEST is how mutmut's trampoline decides which body to call, so
    setting it and running pytest normally is exactly what phase one does --
    minus the test selection, which is the whole point.
    """
    started = time.monotonic()
    try:
        finished = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/",
                "-x",
                "-q",
                "--tb=no",
                "-p",
                "no:randomly",
                "-p",
                "no:random-order",
                "--rootdir=.",
            ],
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


def conftest_hash(test_file, root="mutants"):
    """One hash over the conftests pytest would load for `test_file`.

    A kill is pinned to the test that produced it, and that test's own file is
    not the whole of what produced it. Change a fixture it depends on and it can
    stop failing without a byte of it being touched -- and then the cached kill
    reports a mutant as dead by a test that no longer kills it, which is the
    false green the rest of this file exists to refuse.

    Scoped to the conftests that actually apply -- the rootdir down to the
    test's own directory, which is pytest's own rule -- rather than every
    conftest in the tree. Otherwise an edit to tests/test_tenants/conftest.py
    would invalidate kills from the main invocation, which never loads it.

    Survivors need no equivalent: suite_hash() already covers every .py under
    tests/, conftests included.
    """
    base = Path(root)
    directories = [base]
    for part in Path(test_file).parent.parts:
        directories.append(directories[-1] / part)
    digest = hashlib.sha256()
    for directory in directories:
        path = directory / "conftest.py"
        digest.update(str(path.relative_to(base)).encode())
        # An absent conftest is not the same fact as an empty one, and adding
        # one is a change that can un-kill a test, so the two must not collide.
        digest.update(path.read_bytes() if path.is_file() else b"\0absent")
    return digest.hexdigest()[:16]


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
    with the node id, reusable while the file holding it and the conftests it
    loads are unchanged. Letting it go stale would report a mutant as dead by a
    test that no longer exists, which is the false green this whole workstream
    keeps producing. The conftests are in the pin because the test file alone
    was not enough -- a fixture edit could un-kill the test without touching it.

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
    if entry.get("test_file_hash") != sha(Path(root) / test_file):
        return False
    return entry.get("conftest_hash") == conftest_hash(test_file, root)


def read_equivalents(path=EQUIVALENTS):
    """(name -> reason, problems) for mutants no test can kill.

    Returns problems rather than raising, so a malformed file fails the run
    with every fault listed instead of one at a time.
    """
    try:
        entries = tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}, []
    except (OSError, tomllib.TOMLDecodeError) as error:
        return {}, [f"{path} could not be read: {error}"]

    reasons, problems = {}, []
    for name, entry in entries.items():
        reason = (entry.get("reason") or "").strip() if isinstance(entry, dict) else ""
        if len(reason) < SHORTEST_USEFUL_REASON:
            problems.append(
                f"{name} is exempted with no real reason given. An exemption is a "
                "claim that no test could ever kill it, which needs saying in full."
            )
            continue
        reasons[name] = reason
    return reasons, problems


def module_of(mutant_name):
    """django_overlay.cli.x_main__mutmut_40 -> mutants/django_overlay/cli.py.meta"""
    module = mutant_name.partition("__mutmut_")[0].rpartition(".")[0]
    return Path("mutants") / (module.replace(".", "/") + ".py.meta")


def stale_exemptions(reasons, root="mutants", run=run_full_suite):
    """Exemptions this run can prove are no longer earning their place.

    Only judged where the module was actually mutated -- a shard that does not
    cover cli.py has nothing to say about an exemption in it -- so this is
    quiet in the shards it does not concern and exact in the one it does.

    A kill is confirmed against the whole suite before it retires anything,
    which is the same rule every other verdict here is held to. Phase one runs
    the traced tests, so *any* non-zero exit from that subset is recorded as a
    kill -- including one a flaky test produced. That is not hypothetical:
    models.planning._selective_declared's `getattr(..., False) -> None` mutant
    came back killed on CI and passed all 1,445 tests when the whole suite was
    run against it, because the value is only ever read as an `if` condition
    and nothing can tell the two apart. Retiring an exemption on that evidence
    would have deleted a correct one and demanded a test be written for a
    difference that does not exist.
    """
    problems = []
    for name in sorted(reasons):
        meta = Path(root) / module_of(name).relative_to("mutants")
        if not meta.exists():
            continue
        try:
            codes = json.loads(meta.read_text()).get("exit_code_by_key", {})
        except (OSError, json.JSONDecodeError):
            continue
        if name not in codes:
            problems.append(
                f"{name} is exempted but no longer exists. The code it was about has "
                "changed, so the exemption has to be made again or dropped."
            )
        elif codes[name] in KILLED_CODES:
            code, _, killer = run(name, root=root)
            if code in KILLED_CODES:
                problems.append(
                    f"{name} is exempted but something kills it now"
                    + (f" ({killer})" if killer else "")
                    + ", so the exemption is doing nothing except making the policy "
                    "look smaller than it is."
                )
    return problems


# What phase two needs current copies of inside mutants/: the suite it runs, and
# the scripts and workflow that some of that suite reads.
REFRESHED = ("tests", ".github")


def refresh_copies(root="mutants", sources=REFRESHED):
    """Copy the current tests and scripts over the snapshots inside mutants/.

    mutmut copies these when it runs, and phase two runs pytest from inside
    mutants/ -- so a test written after the last `mutmut run` is simply not in
    the suite phase two executes. It then reports survivors that the current
    suite kills, which is the same stale-copy trap that once had eleven hours of
    runs mutating current Python against old SQL templates.

    .github/ is refreshed for the same reason tests/ is: parts of the suite read
    the shard map and these scripts out of it, and against a stale copy they
    test a version of this file that no longer exists.

    Cheap enough to do unconditionally, and being wrong here means confirming
    against a suite nobody has.
    """
    if not Path(root).is_dir():
        return False
    copied = False
    for source in sources:
        if Path(source).is_dir():
            shutil.copytree(source, Path(root) / source, dirs_exist_ok=True)
            copied = True
    return copied


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
    say(
        f"baseline: the suite does NOT pass inside mutants/ with no mutant active "
        f"(exit {code} after {seconds:.0f}s).\nEvery confirmation would report a kill it "
        "did not measure, so nothing here can be trusted."
    )
    return False


Outcome = collections.namedtuple("Outcome", "confirmed killed hung reused verdicts")


def write_cache(verdicts, path=CACHE):
    """Persist what has been settled so far.

    Written after every verdict rather than at the end, because the end is not
    guaranteed to arrive: a shard that runs into the job's time limit is killed
    where it stands, and a cache written only on the way out would throw away
    hours of confirmations that were already paid for. The next run then starts
    from cold and hits the same limit -- which is the loop this whole cache
    exists to break.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"version": CACHE_VERSION, "verdicts": verdicts}, indent=2, sort_keys=True) + "\n")


def confirm(names, run=run_full_suite, say=print, cache=None, hashes=None, root="mutants", save=write_cache):
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
            # Saved as well as re-run verdicts: what is not written here is
            # dropped from the cache this run leaves behind, so a shard that is
            # killed part-way would lose entries it never had to recompute.
            save(verdicts)
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
                test_file = killed_by.partition("::")[0]
                verdicts[name] = {
                    "killed_by": killed_by,
                    "function_hash": function_hash_for(name, hashes),
                    "test_file_hash": sha(Path(root) / test_file),
                    "conftest_hash": conftest_hash(test_file, root),
                }
        save(verdicts)
    return Outcome(confirmed, killed, hung, reused, verdicts)


def write_report(path, label, confirmed, killed, hung, unchecked, exempt=()):
    payload = {
        "label": label,
        "confirmed": confirmed,
        "killed_by_full_suite": killed,
        "hung": hung,
        "unchecked": unchecked,
        "exempt": list(exempt),
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
    lines = [f"{'shard':<12} {'survived':>8} {'killed here':>12} {'hung':>6} {'unchecked':>10} {'exempt':>7}"]
    lines.append("-" * len(lines[0]))
    for label, payload in rows:
        if "unreadable" in payload:
            lines.append(f"{label:<12} unreadable: {payload['unreadable']}")
            continue
        lines.append(
            f"{label:<12} {len(payload.get('confirmed', [])):>8}"
            f" {len(payload.get('killed_by_full_suite', [])):>12}"
            f" {len(payload.get('hung', [])):>6} {len(payload.get('unchecked', [])):>10}"
            f" {len(payload.get('exempt', [])):>7}"
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
    parser.add_argument("--aggregate", default=None, help="a directory of downloaded per-shard reports; union mode")
    parser.add_argument(
        "--expect-every-shard", action="store_true", help="in union mode, fail unless every shard reported"
    )
    parser.add_argument("--summary", default=None, help="append the table here")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds one confirmation may take")
    parser.add_argument(
        "--names", nargs="*", default=None, help="confirm these mutants instead of the survivors on disk"
    )
    options = parser.parse_args(argv)

    if options.aggregate:
        rows = find_reports(options.aggregate)
        problems = []
        if options.expect_every_shard:
            absent = sorted(expected_shards() - {label for label, _ in rows})
            if absent:
                problems.append(
                    "no phase-two report from shard(s): "
                    + ", ".join(absent)
                    + " -- the union is incomplete, so it cannot be a pass"
                )
        emit(render(rows) if rows else "(no reports)", options.summary, "Mutation — survivors")
        alive = sum(len(payload.get("confirmed", [])) + len(payload.get("hung", [])) for _, payload in rows)
        unreadable = [label for label, payload in rows if "unreadable" in payload]
        if unreadable:
            problems.append("unreadable report(s) from: " + ", ".join(unreadable))
        if problems:
            print("\n" + "\n".join(problems))
            return 1
        if alive:
            print(
                f"\n{alive} mutant(s) survive the whole suite. Either add a test that kills "
                "one,\nor -- if it is genuinely equivalent -- put `# pragma: no mutate` on the "
                "line\nwith a comment saying why."
            )
            return 1
        print("\nno surviving mutants")
        return 0

    if options.names:
        names, unchecked, total = options.names, [], len(options.names)
    else:
        names, unchecked, total = alive_from_meta()

    # "Nothing survived" and "nothing was ever mutated" produce the same empty
    # list. Observed for real: the step that computes the cache key failed, the
    # mutation step was skipped, mutants/ did not exist, and this reported a
    # clean shard. The same green-out-of-nothing this file exists to prevent.
    if not total:
        print(
            f"{options.label}: no mutants found under mutants/ at all. `mutmut run` did not "
            "get as far as\ngenerating them, so there is nothing here to confirm and nothing "
            "to pass."
        )
        return 1
    # Read through the module attribute rather than the default, which binds
    # at definition time and cannot be pointed elsewhere.
    reasons, problems = read_equivalents(EQUIVALENTS)
    problems += stale_exemptions(reasons)
    if problems:
        print("\n".join(problems))
        print("\nSee .github/mutation-equivalents.toml.")
        return 1

    exempt = [name for name in names if name in reasons]
    if exempt:
        names = [name for name in names if name not in reasons]
        print(f"{len(exempt)} mutant(s) exempted as equivalent, and not re-run:")
        for name in exempt:
            print(f"  {name}: {reasons[name].splitlines()[0]}")

    runner = lambda name: run_full_suite(name, timeout=options.timeout)  # noqa: E731
    if not names:
        print(f"{options.label}: nothing survived phase one, so there is nothing to confirm")
    else:
        print(f"{options.label}: confirming {len(names)} survivor(s) against the whole suite")
        if refresh_copies():
            print(f"refreshed {'/, '.join(REFRESHED)}/ inside mutants/")
        if not baseline_is_clean(run=runner):
            return 1
    outcome = confirm(names, run=runner)
    payload = write_report(REPORT, options.label, outcome.confirmed, outcome.killed, outcome.hung, unchecked, exempt)
    write_cache(outcome.verdicts)
    if outcome.reused:
        print(
            f"{len(outcome.reused)} of {len(names)} verdict(s) came from the cache, "
            "each still pinned to the test that produced it"
        )
    emit(render([(options.label, payload)]), options.summary, f"Mutation — {options.label}")

    if unchecked:
        print(
            f"\n{len(unchecked)} mutant(s) have no phase-one verdict at all -- `mutmut run` "
            "did not\nfinish. Nothing here can be trusted until it does."
        )
        return 1
    if outcome.confirmed or outcome.hung:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
