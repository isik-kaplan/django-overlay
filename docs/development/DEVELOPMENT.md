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
POSTGRES_USER=postgres uv run mutmut run     # ~3s per mutant
uv run mutmut browse                          # inspect what lived
uv run mutmut show <mutant-name>              # the diff for one mutant
```

**Policy: no mutant survives.** A mutant that lives is a change to the
package that no test objects to — either a test is missing or the code is
dead. CI enforces this (`mutation` job in `.github/workflows/tests.yml` via
`.github/scripts/check_mutants.py`), counting `survived`, `suspicious`,
`timeout`, `no_tests` and `segfault` all as alive.

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

Two configuration notes, both in `[tool.mutmut]` and both load-bearing:

- The full suite runs for every mutant (`pytest_add_cli_args = ["tests/"]`).
  mutmut's own per-mutant test selection under-selects badly for this
  package.
- `django_overlay/operations.py` is excluded. Its code runs inside
  pytest-django's session-scoped database setup rather than inside a test,
  where mutmut cannot swap the mutant in — every mutant there would survive
  no matter how many tests were added. Adding tests that apply migrations
  from inside a test body would let this exclusion come out.
