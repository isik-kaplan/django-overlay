# Benchmarks

Everything under `benchmark/` is for people working on the library from a
source checkout. None of it ships: `pyproject.toml` restricts the wheel to the
`django_overlay` package, so an installed copy carries only a shim that
explains where the real command lives.

```bash
uv run django-overlay benchmark --help
uv run django-overlay benchmark --list-suites
uv run django-overlay benchmark --interactive
```

## The short version

```bash
# a couple of minutes, enough to see the shapes
uv run django-overlay benchmark --scale 0.1

# the real thing — the size the interesting pathologies appear at
uv run django-overlay benchmark --scale 1.0 --save-results --label before-my-change
```

The first run starts a Postgres in docker compose and builds the graph. The
build is the slow part and it is cached, both in a `bench_cache` schema inside
the database and in a named docker volume, so the second run skips it.

## What it measures

Every suite compares an overlay view against a plain, non-overlay table holding
identical rows with identical indexes. The plain column is the floor — not
something the overlay can reach, but the thing that says whether a ratio is the
view's fault or the query's.

| suite | question |
|---|---|
| `shapes` | the view against its mirror, in raw SQL: lookups, pages, counts, joins |
| `hybrid` | is `view -> plain` cheap where `view -> view` is not? |
| `selectivity` | where the cliff is, sweeping two m2m conditions narrow to broad |
| `aggregation` | a twenty-aggregate summary panel, written five ways |
| `staged` | resolving a saved search leaf by leaf instead of all at once |
| `set_algebra` | can the leaf-by-leaf strategy stay in SQL? |
| `ban` | what `DJANGO_OVERLAY_FORCE_HASH_JOINS` is worth, and what it costs |
| `hops` | does the ban still hold at three and four conditions? |
| `fence` | where materialising an m2m scope stops paying |
| `partitions` | what declaring a source partitioned prunes away, swept by partition count |

`--smoke` runs `shapes` and `ban`, which is what CI does on every push.

## Turning the optimisations off

The library carries five query optimisations, all on by default. Each one has a
flag, so the question the benchmark answers is not only "overlay against a plain
table" but "this rewrite against no rewrite":

```bash
uv run django-overlay benchmark --scale 1.0 --save-results   # every one on
uv run django-overlay benchmark --scale 1.0 --no-optimisations
```

| flag | setting it moves |
|---|---|
| `--no-rewrite-traversals` | `DJANGO_OVERLAY_REWRITE_TRAVERSALS` |
| `--no-redirect-select-related` | `DJANGO_OVERLAY_REDIRECT_SELECT_RELATED` |
| `--no-force-hash-joins` | `DJANGO_OVERLAY_FORCE_HASH_JOINS` |
| `--no-array-subquery-in` | `DJANGO_OVERLAY_ARRAY_SUBQUERY_IN` |
| `--no-m2m-fence` | `DJANGO_OVERLAY_M2M_FENCE` |

`--no-optimisations` turns them all off. An individual flag still wins against
it, so `--no-optimisations --force-hash-joins` prices the nested-loop ban on its
own rather than all of them together.

`ARRAY_SUBQUERY_IN` and `M2M_FENCE` were one flag until they were split. It gated
two unrelated things — the `fk__in=<subquery>` rewrite and the m2m fence — so
turning it off to spare one broad foreign-key filter also unfenced every m2m
traversal, giving away 306.6ms → 0.4ms selective and 7,896.5ms → 105.9ms broad to
fix something else. The fence's flag gates whether the fence is *added*, not how
it compiles: a fence compiled as a plain `IN` is the one combination with no
argument for it, carrying the extra semi-join and none of the benefit. The names live in `benchmark/switches.py`,
which the CLI, the settings module and the environment record all read, and a
test asserts each flag reaches the library's own gate — a flag that moves a
setting nothing reads reports the default arm under the other arm's name.

Comparing against `master` is **not** the same measurement. None of these
mechanisms exists there, and neither does this harness.

Two things follow from the switches being recorded in each run's environment:

- a switch difference **does not** block the delta column, unlike every other
  environment key. That comparison is the measurement. But the note says which
  optimisation moved, because a `+21950%` column from a flag and a `+21950%`
  column from a regression look identical.
- the default `--label` carries the arm, so both halves of an A/B taken at one
  commit do not overwrite each other.

Mutation testing cannot reach any of this: every mutant runs under the default
settings, so the non-default configurations are invisible to it by construction.
`tests/test_optimisations_off.py` is what covers them — every on/off combination,
differential against all-on, asserting identical rows rather than identical SQL.

None of these flags make the library adapt at runtime, and none ever will.
Every optimisation is decided by **query shape** alone: which models are joined,
how many overlay views are involved, whether there is a `LIMIT`, whether an rhs
is a literal or a subquery. Nothing consults row counts, `pg_class.reltuples` or
statistics, because SQL that depends on database state is SQL you cannot read off
the code — the same query would plan differently on two machines and a stale
`ANALYZE` would change behaviour silently. Where the right answer genuinely
depends on data size, the library picks a static default and offers a manual
per-query opt-in instead; see `OverlayFencedIn` for the one case, reachable as
`filter(pk__overlay_fenced_in=<queryset>)`.

## Scale

`--scale 1.0` is 1,000,000 people, 800,000 addresses, and 3,000,000 label
links. Runtime does **not** grow linearly with it, because the shapes that cap
out at the larger sizes are exactly the ones the suites exist to measure — the
`ban` suite is about 50 seconds at 0.3 and about eight minutes at 1.0.

Scale 0.3 is not a substitute for 1.0. Two of the shapes the nested-loop ban
exists for are healthy at 300,000 rows and take 27 seconds at 1,000,000, so a
run at 0.3 reports the regressions the ban prevents as fine.

## The runtime budget

`--max-runtime` defaults to `1h` and is enforced, not advisory. Before a suite
starts, the runner asks whether the remaining budget can afford it; if not the
suite is skipped and says so in the output and in the closing banner. A suite is
never started knowing it will overrun, because that blows the ceiling *and*
produces a partial table that compares with nothing.

The ceiling is also checked *inside* a suite, before every measurement. The
statement cap bounds how long Postgres will spend on a query, but not how long
Django spends marshalling a third of a million primary keys into one — which
the `staged` suite does at scale 1.0. Past the deadline, remaining cells read
`skipped` rather than a duration, and the suite is reported as **cut short**.

Checking before a measurement cannot stop one already running, and that
distinction cost a CI run: `staged` at scale 0.3, estimated at twenty-one
seconds, spent thirty-seven minutes inside a single execution and was killed by
the job timeout with twenty-two minutes of budget unspent. So each execution
also carries a ceiling of its own — six times the statement cap, floor thirty
seconds, never more than what is left of the budget — enforced with a signal,
which is the only thing that reaches into a call already in progress. Past it,
the cell reads `gave up`.

Five cell states, and they are not interchangeable:

| cell | means |
|---|---|
| `412ms` | measured |
| `>10s` | ran past the statement cap — a lower bound, not a number |
| `gave up` | the wall clock ran out mid-execution, not the cap |
| `conn lost` | the connection broke; nothing was measured |
| `skipped` | never run: the budget was gone |

`conn lost` exists because the first two used to collapse into one. A statement
timeout and a connection with an unconsumed result both reach the harness as
`OperationalError`, so a run where the connection broke mid-suite printed five
rows of `>10s did not finish` for queries that were never sent — five failures
dressed as five measurements. The harness now reads the SQLSTATE: `57014` and
`55P03` are the caps doing their job, anything else means the connection is
gone, so it is closed, the cell says so, and the reason is written to stderr as
`LOST CONNECTION …` where the CI summary picks it up.

The estimate is printed before anything slow happens — before docker, before the
schema, before the graph — so a `--scale 3.0` typed at four in the afternoon
says "this is about forty minutes" at second one rather than at minute five.
Over the ceiling, it asks before starting unless `--yes`.

Estimates come from measured points per suite in `benchmark/estimates.py` and
are interpolated. If you take a run at a new scale, update them.

## Saving and comparing

```bash
uv run django-overlay benchmark --scale 1.0 --save-results --label before
# ... make a change ...
uv run django-overlay benchmark --scale 1.0            # delta column appears
uv run django-overlay benchmark --clear-results
```

Later runs pick the most recent saved run up automatically — the flag is for
producing a baseline, not for using one. Two rules keep the deltas honest:

- a run taken in a **different environment is not compared**. Postgres major
  version, `work_mem`, `shared_buffers`, cores, scale, share and the statement
  cap all have to match. Comparing a 3M/PG17 run against a 1M/PG16 one is worse
  than offering no comparison: it dresses noise up as a regression.
- a change under **20%** is not shown at all. The harness cannot resolve a 5%
  move, and printing one invites a hunt for measurement error.
- a run with **different optimisations enabled is** compared, and told to say
  so. See the section above.

`benchmark/results/` is gitignored. Saved runs are machine-specific by
construction.

## The database

By default the CLI starts one in docker compose:

```bash
uv run django-overlay benchmark --postgres-version 16 --work-mem 16MB
uv run django-overlay benchmark --down            # stop it, keep the data
```

The volume is named per major version and survives `down`, so the loaded graph
outlives the container. A data directory initialised by one major version will
not start under another, which is why the version is in the volume name.

To use a database you already have — which is what CI does — pass a URL:

```bash
uv run django-overlay benchmark --database-url postgres://user:pass@host:5432/db
```

That path skips docker entirely. The benchmark itself runs the same code either
way; only the plumbing differs.

Note that `work_mem` and `shared_buffers` are flags rather than defaults you can
ignore. Both have moved headline numbers in this project by more than the
effects being measured, and every saved run records them.

## In CI

`.github/workflows/benchmarks.yml` runs three tiers, because the cost of a
benchmark and the confidence it buys are far apart at different scales.

| tier | when | scale | gates? |
|---|---|---|---|
| `smoke` | every PR + master push | 0.05 | **yes** — rows differing |
| `standard` | master push + **maintainers'** PRs | 0.3 | rows only; timings report |
| `deep` | `workflow_dispatch` only | 1.0 (input) | nothing |

Only the first thing is ever a failure: the overlay and a plain table holding
identical rows returning different answers. A timing on a shared runner is not
a pass/fail signal and is never treated as one.

**`standard` is not the top tier, and 0.3 is not proof.** Two of the shapes the
nested-loop ban exists for are healthy at 300,000 rows and take 27 seconds at
1,000,000 — summary counts 81ms → 26,829ms, scope-as-subquery 107ms → 26,250ms.
So 0.3 is a fast regression check on every merge; anything being reasoned about
seriously wants a `deep` run at 1.0 behind it, which is a button, not a
schedule.

`standard` and the mutation job in `tests.yml` are limited to maintainers via
each workflow's `gate` job, which reads GitHub's own `author_association`.
An outside contributor's PR runs tests and the smoke tier and nothing more —
mutation is up to two hours, and a check that never finishes is one everybody
learns to ignore. Both policies are enforced on master after merge.

Tables go to `$GITHUB_STEP_SUMMARY` as markdown. GitHub has no `warning` job
status, so `.github/scripts/benchmark_warnings.py` emits `::warning::`
annotations and repeats the budget at the very end of the log.

## Where things live

```
benchmark/
  cli.py         the click command
  runner.py      loads the graph, walks the suites, enforces the budget
  harness.py     timing, tables, the budget
  graph.py       the data generator and table map  <- shared, do not fork
  estimates.py   measured runtimes, for the prediction
  results.py     saved runs and the delta column
  environment.py what the machine was
  docker.py      compose lifecycle
  settings.py    Django settings for a benchmark run
  switches.py    the four optimisation flags, and the settings they move
  suites/        one module per suite
  compose/       docker-compose.yml
```

The Django models are **not** here — `BenchPerson`, `PlainPerson` and the rest
live in `tests/testapp/models.py`, because the permanent test suite depends on
them too and two definitions of one schema is worse than the import. `graph.py`
is the single source of truth for the data itself: the exploratory probes under
`tests/probe_*.py` build their graph from it as well, so the CLI and the probes
cannot drift into measuring different tables.

## The probes

`tests/probe_*.py` are lab notes — one-off investigations, kept because their
reasoning is worth reading, not because they are maintained. pytest's default
collection pattern skips them, and they build their graph from
`benchmark/graph.py` so they cannot drift from the CLI on the *data*.

Eight of them **became** the suites above and were then deleted, because two
copies of one measurement is a copy that will eventually lie:

| was | is now |
|---|---|
| `probe_bench_graph.py` | `benchmark/suites/shapes.py` |
| `probe_hybrid_plain_target.py` | `benchmark/suites/hybrid.py` |
| `probe_selectivity_sweep.py` | `benchmark/suites/selectivity.py` |
| `probe_aggregation.py` | `benchmark/suites/aggregation.py` |
| `probe_staged_resolution.py` | `benchmark/suites/staged.py` |
| `probe_set_algebra_in_sql.py` | `benchmark/suites/set_algebra.py` |
| `probe_hash_join_ban.py` | `benchmark/suites/ban.py` |
| `probe_hop_scaling.py` | `benchmark/suites/hops.py` |

Their commentary went with them — each suite module opens with the reasoning
its probe carried. Git history has the originals if you want the diff.

Nothing now measures the same thing twice, and three things keep it that way:

- `tests/test_benchmark_tooling.py` asserts every module in `benchmark/suites/`
  is registered in `SUITE_NAMES`, conforms to the interface, and has a runtime
  estimate — so a suite cannot be added, renamed or broken unnoticed by the
  ordinary test run;
- the CI smoke job runs two suites on every push, which is what the probes were
  added to CI for in the first place;
- `benchmark/graph.py` is the only data generator, so the remaining probes and
  the CLI cannot end up describing different tables.
