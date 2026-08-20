"""Which source file each mutation shard owns, and how a shard is selected.

A full mutation pass is one job per shard, and every shard runs the *whole*
test suite -- only the set of files it mutates differs. That split is the one
that keeps the answer intact. Sharding the tests instead would be unsound: a
mutant is killed if *any* test kills it, so a shard holding half the suite
would report "survived" for mutants the other half kills, and mutmut has no way
to intersect verdicts across runs. Sharding the mutants gives each shard's
verdicts bit-for-bit what a single job would have produced, and the union is
the full result.

Shards are subsystems rather than an even slicing, so a red shard names
something a person recognises. They are not equal in size, because
`only_mutate` matches files rather than line ranges -- a shard can only be as
fine as the file layout is. That is why django_overlay/models/ is a package and
not a 1,227-line module: as one file it was 42% of the mutants in one job,
which measured 5.5 hours against a 6-hour cap. Split by subject, its three
shards are ~86, ~64 and ~49 minutes.

`only_mutate` is deliberately excluded from mutmut's config fingerprint --
"new mutants are born uncached and dropped ones simply stop being walked, so
they need no result invalidation" -- so varying it per shard costs nothing in
cached results. That is what makes this split free.

Usage, in CI and identically by hand:

    rm -rf mutants                                     # see below, if switching
    python .github/scripts/mutation_shards.py models
    uv run mutmut run --max-children 1

    python .github/scripts/mutation_shards.py --list
    python .github/scripts/mutation_shards.py --clear   # mutate everything again

`rm -rf mutants` before *switching* shards, and it is not optional. mutmut
builds that tree once and does not add scaffolding for a file a later
`only_mutate` newly includes, so a run scoped to a shard whose files were never
mutated walks the previous shard's mutants and reports their verdicts. Three
shards run back to back here all reported the same 325 mutants and 321 kills.
check_mutants.tree_problems() now fails the run instead, naming the files -- but
the tree still has to be removed to get an answer.
"""

import sys
from pathlib import Path


PYPROJECT = Path("pyproject.toml")

# shard -> the files it mutates. tests/test_mutation_shards.py asserts this
# covers django_overlay/ exactly once, so adding a module fails the suite until
# it is assigned somewhere. A hand-maintained split without that check quietly
# stops testing whatever was added last.
SHARDS = {
    # OverlayQuerySet: the ORM surface, and the biggest shard at ~320 mutants.
    # QuerySet._update, count(), select_related() and bulk_create() are here.
    "queryset": [
        "django_overlay/models/queryset.py",
    ],
    # OverlayQuery: the rewrites underneath that surface -- the traversal
    # semi-join, the m2m fence, Query.build_lookup and Query.get_compiler.
    "query": [
        "django_overlay/models/query.py",
    ],
    # The rest of the model machinery: the nested-loop ban and the counting it
    # rests on, OverlayMeta, and the metaclass that splits one declaration in
    # two. __init__.py is assigned rather than exempt -- it re-exports rather
    # than being empty, and `__all__` is a real claim about the public surface.
    "models": [
        "django_overlay/models/__init__.py",
        "django_overlay/models/base.py",
        "django_overlay/models/meta.py",
        "django_overlay/models/planning.py",
    ],
    # The declaration-time diagnostics.
    "checks": [
        "django_overlay/checks.py",
        "django_overlay/apps.py",
    ],
    # makemigrations and the two operational commands.
    "commands": [
        "django_overlay/management/commands/makemigrations.py",
        "django_overlay/management/commands/resync_overlay_views.py",
        "django_overlay/management/commands/show_source_indexes.py",
    ],
    # Field descriptors and the identity/uniqueness rules around them.
    "fields": [
        "django_overlay/fields.py",
        "django_overlay/uniqueness.py",
        "django_overlay/uuid7.py",
        "django_overlay/sources.py",
        "django_overlay/exceptions.py",
    ],
    # Everything that emits or inspects DDL. operations.py belongs here by
    # subject but is excluded from mutation entirely -- see do_not_mutate in
    # pyproject.toml and KNOWN_ISSUES/09.
    "ddl": [
        "django_overlay/sql.py",
        "django_overlay/sync.py",
        "django_overlay/constraints.py",
        "django_overlay/strategies.py",
        "django_overlay/_templating.py",
        "django_overlay/introspection.py",
    ],
    # The shipped entry point.
    "cli": [
        "django_overlay/cli.py",
    ],
}

# Files no shard owns, each with the reason. Anything here is exempt from the
# coverage assertion in the test; a reason is required so the exemption list
# cannot grow by accident.
NOT_SHARDED = {
    "django_overlay/__init__.py": "empty",
    "django_overlay/management/__init__.py": "empty",
    "django_overlay/management/commands/__init__.py": "empty",
    "django_overlay/operations.py": "do_not_mutate -- invisible to mutmut, see KNOWN_ISSUES/09",
}

MARKER = "# --- only_mutate, written by .github/scripts/mutation_shards.py ---"


def render(shard):
    """The TOML lines that scope a run to `shard`."""
    paths = ",\n".join(f'    "{path}"' for path in SHARDS[shard])
    return f"{MARKER}\nonly_mutate = [\n{paths}\n]\n"


def clear(pyproject=PYPROJECT):
    """Remove the generated block, so a run mutates everything again.

    Selecting a shard edits a tracked file. Somebody who ran one locally needs
    a way back that is not `git checkout pyproject.toml`, because that would
    also throw away whatever else they were editing.
    """
    lines, _ = _without_generated_block(pyproject)
    pyproject.write_text("".join(lines))


def _without_generated_block(pyproject):
    """(lines, whether a block was there) -- pyproject.toml with ours removed."""
    kept, skipping, found = [], False, False
    for line in pyproject.read_text().splitlines(keepends=True):
        if line.startswith(MARKER):
            skipping, found = True, True
            continue
        if skipping:
            # The generated block is the marker, `only_mutate = [`, its paths,
            # and the closing bracket. It ends at that bracket.
            if line.startswith("]"):
                skipping = False
            continue
        kept.append(line)
    return kept, found


def select(shard, pyproject=PYPROJECT):
    """Rewrite pyproject.toml so `mutmut run` covers only this shard.

    mutmut reads its configuration from [tool.mutmut] in pyproject.toml and
    nowhere else -- there is no environment override -- so selecting a shard
    means editing the file. Written immediately after the section header, which
    is valid wherever in the section it lands, and replacing any block a
    previous call left behind so this is idempotent.
    """
    if shard not in SHARDS:
        raise SystemExit(
            f"unknown shard {shard!r} -- known shards: {', '.join(sorted(SHARDS))}"
        )

    kept, _ = _without_generated_block(pyproject)
    try:
        at = kept.index("[tool.mutmut]\n") + 1
    except ValueError:
        raise SystemExit("pyproject.toml has no [tool.mutmut] section") from None

    kept.insert(at, render(shard))
    pyproject.write_text("".join(kept))
    return shard


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(__doc__)
        return 0
    if argv[0] == "--clear":
        clear()
        sys.stdout.write("mutation shard cleared: every module will be mutated\n")
        return 0
    if argv[0] == "--list":
        for shard, paths in SHARDS.items():
            sys.stdout.write(f"{shard:<10}{len(paths)} file(s)\n")
        return 0
    select(argv[0])
    sys.stdout.write(
        f"mutation shard {argv[0]!r}: {len(SHARDS[argv[0]])} file(s) will be mutated\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
