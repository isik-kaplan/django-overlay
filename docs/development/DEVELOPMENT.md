# Running the tests

Needs a real Postgres instance — no SQLite path.

```bash
uv sync
POSTGRES_USER=postgres uv run pytest
```

Covers `NEGATIVE_ID`/`UUID4`/`UUID7_POLYFILL` (native `UUID7` needs Postgres
18+, covered only by pure-Python/SQL-string unit tests) across a
People/Address/Phone model set: both M2M styles, a one-to-one and a plain FK
bonus relation, and the checks that reject unsafe FK/M2M usage.

`tests/test_tenants/` proves the same mechanism under `django_tenants`
schema-per-tenant multi-tenancy — separate settings module and database:

```bash
DJANGO_SETTINGS_MODULE=tests.django_tenants_settings POSTGRES_USER=postgres \
  uv run pytest tests/test_tenants -o addopts="" --create-db
```

A few tests use `hypothesis` for property-based fuzzing instead of fixed
examples — identifier quoting, and the migration-detection dedup guards.

The main suite (not `test_tenants`) is 100% line+branch coverage, enforced:

```bash
POSTGRES_USER=postgres uv run pytest --cov=django_overlay --cov-report=term-missing
```

Fails if coverage drops below 100% (`fail_under` in `pyproject.toml`).

## Mutation testing

100% line+branch coverage says every line ran, not that anything checked what
it did. Mutation testing closes that gap: `mutmut` makes small changes to
`django_overlay/` and expects the suite to fail for each one.

```bash
POSTGRES_USER=postgres uv run mutmut run --max-children 1      # phase one
POSTGRES_USER=postgres uv run python .github/scripts/confirm_survivors.py
uv run mutmut show <mutant-name>              # the diff for one mutant
uv run mutmut browse                          # inspect what lived
```

It runs in two phases, and the split is what makes a full pass affordable.
Phase one is `mutmut run`, which tests each mutant against only the tests its
tracing associates with it — a couple of seconds each. Tracing under-selects
here, so phase one reports survivors that aren't real, but it can only ever err
that way: a test that never ran cannot have killed a mutant that lived, and a
kill is a kill whichever tests produced it. Phase two takes each survivor and
runs the **whole** suite against it, which is what the report gates on.

Only survivors pay for a full pass. That is the difference between four of the
six shards needing ten to twenty hours and all six fitting inside one:

| | per mutant | 2,209 mutants |
|---|---|---|
| whole suite for every mutant | ~32s local, ~97s CI | 19.6h local, 59h CI |
| traced tests, then confirm | ~2s, plus a full pass per survivor | ~1.2h local |

Phase two caches its verdicts, in `mutants/mutmut-confirmed-cache.json`, which
travels between CI runs inside the same cache as phase one's. Without it a shard
pays for every survivor again on every push, however small the change.

Every verdict needs the mutated function unchanged. What else it needs depends
on what the verdict claimed:

| verdict | claims | reusable while |
|---|---|---|
| killed | *this test objected* | the file holding that one test is unchanged |
| survived | *nothing objected* | no test file anywhere has changed |

A kill is pinned to the node id recorded at the time, so weakening or deleting
that test retires the verdict; a kill nobody can attribute is never cached at
all. A survivor is a claim about the whole suite, so the whole suite is what
invalidates it — coarser on purpose, because there is no single test to point
at when the finding is that none of them objected.

`--max-children 1` is mandatory, not a preference. mutmut otherwise forks one
child per CPU, and every child talks to the same Postgres test database: they
race each other's schema, tests fail for reasons unrelated to the mutation, and
mutmut reads those failures as mutants killed. A run without it reports a
perfect score it did not measure. It cannot go in `[tool.mutmut]` — mutmut
accepts it only as a flag and ignores unknown config keys silently — so
`tests/test_mutmut_config.py` guards the config against claiming otherwise. For
the same reason, nothing else may touch the test database while a run is going,
including a stray `pytest` in another terminal.

**Policy: no mutant survives.** A mutant that lives is a change to the
package that no test objects to — either a test is missing or the code is
dead. CI enforces this (`.github/workflows/mutation.yml`): `check_mutants.py`
reports phase one and checks that it ran at all, and `confirm_survivors.py`
gates on what survives the full suite. Phase one counts `survived`,
`suspicious`, `timeout`, `no_tests` and `segfault` all as alive, so all of them
reach phase two; a mutant with no verdict at all fails the build outright,
because "nothing survived" and "nothing ran" produce the same numbers.

### Shards

A full pass is one CI job per subsystem, each mutating a slice of
`django_overlay/` and running the **whole** suite. Reproduce any one of them:

```bash
uv run python .github/scripts/mutation_shards.py --list
uv run python .github/scripts/mutation_shards.py models   # rewrites [tool.mutmut]
POSTGRES_USER=postgres uv run mutmut run --max-children 1
POSTGRES_USER=postgres uv run python .github/scripts/confirm_survivors.py --label models
```

Sharding the *mutants* rather than the tests is the only split that keeps the
answer: a mutant is killed if *any* test kills it, so a shard holding half the
suite would report "survived" for mutants the other half kills, and mutmut has
no way to intersect verdicts across runs. Because every shard runs everything,
its verdicts are what a single job would have produced and the union is the
full result. `only_mutate` is excluded from mutmut's config fingerprint, so
switching shards invalidates no cached verdict.

`tests/test_mutation_shards.py` asserts the map covers the package exactly
once, in the ordinary test run — add a module without assigning it to a shard
and `pytest` fails with its name, rather than the module silently never being
mutated.

The `models` shard is 42% of the mutants on its own and `only_mutate` matches
files rather than line ranges, so it cannot be split further. It is the
critical path.

The one legitimate exit is a genuinely equivalent mutant — one whose mutated
form is indistinguishable from the original. Mark it in place and say why:

```python
name = value[:63]  # pragma: no mutate  (Postgres truncates identifiers itself)
```

### The half mutmut can't reach

mutmut forks a pre-warmed parent and only selects the mutant in the child, so
anything whose effect happens at *import* time — the metaclass, `uniqueness`'s
narrowing, every migration operation — has already run against the original
code by the time a mutant is chosen. Those mutants are reported as survived no
matter how good the tests are.

`tests/probe_unreachable_mutants.py` covers that half the crude way: edit the
source, run the suite, put it back.

```bash
POSTGRES_USER=postgres uv run python tests/probe_unreachable_mutants.py
POSTGRES_USER=postgres uv run python tests/probe_unreachable_mutants.py sql operations
```

~30 hand-chosen mutations, one suite run each, exit code = number of unexpected
results. It runs in CI. Unlike mutmut's generated mutants these are picked for
meaning, so a survivor is always worth reading — and each one's `old` string
must match the source exactly once, so a refactor that moves the code reports
STALE rather than silently testing nothing.

A mutation that provably *cannot* be killed is marked `"equivalent"` in the
list with a comment saying why, and the harness then asserts it survives — so
if the reasoning ever stops holding, that shows up too.

Three configuration notes, all in `[tool.mutmut]` and all load-bearing:

- The full suite runs for every mutant (`pytest_add_cli_args = ["tests/"]`).
  mutmut's own per-mutant test selection under-selects badly for this
  package.
- The per-mutant wall limit is a flat ~15 minutes
  (`timeout_multiplier = 1.0`, `timeout_constant = 900`), not the default
  `(traced + 1) × 15`. The default is computed from the tests mutmut's tracing
  associates with the mutant, but the line above makes the whole suite run
  regardless — so the limit came out near 16s against a full pass of 69–97s on
  a CI runner. A killed mutant exits at the first failing test and never
  notices; a survivor has to reach the end, so it was cut off and filed as
  `timeout`. `survived` was unreachable, and a run reporting 0 survivors and
  230 timeouts was reporting 230 survivors it could not name. Changing either
  value resets only timeout verdicts, never killed ones.
- `django_overlay/operations.py` is excluded. Its code runs inside
  pytest-django's session-scoped database setup rather than inside a test,
  where mutmut cannot swap the mutant in — every mutant there would survive
  no matter how many tests were added. Adding tests that apply migrations
  from inside a test body would let this exclusion come out.

## Benchmarks

`uv run django-overlay benchmark` — eight suites comparing the overlay against
plain tables holding identical rows, run against a Postgres the command starts
in docker compose. Not shipped in the wheel; see
[BENCHMARKS.md](BENCHMARKS.md) for the suites, the runtime budget and how saved
runs are compared.

```bash
uv run django-overlay benchmark --list-suites
uv run django-overlay benchmark --scale 0.1          # a couple of minutes
uv run django-overlay benchmark --interactive
```
