# Performance

Measured by three probes that load the real `Person` overlay model — real view,
real `UNION ALL`, real anti-join, real `INSTEAD OF` triggers — and compare it
against `bench_plain`, an ordinary table holding the same rows with the same
indexes. Run them yourself:

```bash
POSTGRES_USER=postgres uv run pytest tests/probe_ratio.py -s -q -o addopts="" --no-cov
POSTGRES_USER=postgres uv run pytest tests/probe_scale.py -s -q -o addopts="" --no-cov
POSTGRES_USER=postgres uv run pytest tests/probe_join_scale.py -s -q -o addopts="" --no-cov

OVERLAY_BENCH_SHARES=0.01,0.25 POSTGRES_USER=postgres uv run pytest tests/probe_ratio.py ...
OVERLAY_BENCH_ROWS=2000000 POSTGRES_USER=postgres uv run pytest tests/probe_scale.py ...
```

None is in CI or in the default suite — they load hundreds of thousands of rows.

**On reading ratios.** The plain-table baseline for a point lookup or a keyset
page is 0.1–0.4ms, and it moves by that much between runs, so the ratio against
it swings wildly (the same query measured 19× and 53× on consecutive runs) while
the overlay's own absolute time barely moved. Judge the absolute milliseconds;
treat the ratios as an order of magnitude, not a measurement.

## What to write, and what not to

Every number here is from `django-overlay benchmark --scale 1.0` — 1,000,000
people, `BenchPerson` against the `PlainPerson` mirror holding identical rows,
every optimisation at its default. The `plain` column is that mirror. Reproduce
with:

```bash
uv run django-overlay benchmark --suite ban --suite hops --suite fence --scale 1.0
```

Read the absolute milliseconds first. The ratios matter when they cross an order
of magnitude, and the mirror is a floor the overlay cannot reach rather than a
target it should hit.

### Fast — at or better than the mirror

```python
# 18ms (plain 22ms) — one m2m hop, selective. The fence is worth ×12 here.
BenchPerson.objects.filter(addresses__city="city0").values("pk").distinct().count()

# 41ms (plain 51ms) — a 5,000-row scope
BenchPerson.objects.filter(addresses__city__in=[f"city{n}" for n in range(10)])

# 63ms — a selective ordered page: LIMIT plus a narrow scope is the best case
list(BenchPerson.objects.filter(addresses__city="city0").order_by("id")[:200])

# 223ms (plain 456ms) — m2m to a *plain* table, faster than the mirror itself.
# The plain side carries statistics, so the estimate is never blind.
BenchPerson.objects.filter(labels__kind="volunteer")
```

### OK — a few hundred ms, 2–4× the mirror

```python
# 220ms (plain 96ms) — a 50,000-row scope, no LIMIT. The fence still wins ×1.7.
BenchPerson.objects.filter(addresses__city__in=[f"city{n}" for n in range(100)])

# 952ms (plain 235ms) — one hop matching 186,666 rows. Broad, but linear.
BenchPerson.objects.filter(phones__kind="mobile").values("pk").distinct().count()

# 301ms on a 3,000,000-row view — a bare count, decomposed into two counts
BenchPerson.objects.count()

# 19ms (plain 5ms) — no join at all
BenchPerson.objects.filter(city="city42").count()
```

### Slow — seconds. Works, but design around it

```python
# 752ms (plain 28ms, ×27) — two m2m hops. The nested-loop ban is what makes
# this finish at all: unbanned it runs past 30s.
BenchPerson.objects.filter(addresses__city="city0", phones__kind="mobile") \
    .values("pk").distinct().count()

# 3,851ms (plain 34ms, ×113) — three hops
BenchPerson.objects.filter(addresses__city="city0", phones__kind="mobile",
                           emails__domain="example.com")

# 1,202ms (plain 24ms, ×50) — two hops with a LIMIT; 7,935ms at four hops
list(BenchPerson.objects.filter(addresses__city="city0",
                                phones__kind="mobile").order_by("id")[:200])

# 539ms (plain 2ms, ×270) — an ordered page over a 50,000-row scope. The worst
# ratio on this page: the page needs 200 rows and the fence materialises all
# 50,000 of them.
list(BenchPerson.objects.filter(addresses__city__in=[f"city{n}" for n in range(100)])
     .order_by("id")[:200])

# 1,680ms, against 862ms with the fence off — a 500,000-row scope is the one
# case where the fence is a net loss. Narrow the scope; see below.
BenchPerson.objects.filter(addresses__city__isnull=False)
```

### Never

Most of these you cannot reach by accident — the library rewrites or refuses
them. They are listed because the settings that re-enable them are documented,
and because the last one is a crash rather than a slow query.

```python
# 1. Joining two overlay views directly: 76×–1304× the mirror. You get a
#    prefetch instead, so this is only reachable by turning the redirect off.
BenchPerson.objects.select_related("address")

# 2. select_related() after a set operation — raises OverlayConfigurationError,
#    because it cannot be routed and the join it would emit is the 76×–1304× one.
BenchPerson.objects.union(other).select_related("address")

# 3. .iterator() with no chunk_size — refused, because the prefetch that stands
#    in for select_related() needs a batch to work over.
BenchPerson.objects.filter(...).iterator()          # pass chunk_size=

# 4. Two m2m hops with DJANGO_OVERLAY_FORCE_HASH_JOINS = False — past 30s at
#    1,000,000 people, where banned it is 752ms.

# 5. A scope-as-subquery with DJANGO_OVERLAY_M2M_FENCE = False. Not slow: it
#    takes the server down. The plan builds a Parallel Hash over 7,950,000,000
#    estimated rows for a 200-row answer, that node's tuplestore lives in
#    dynamic shared memory, and the backend is killed with signal 9 — the whole
#    instance goes into recovery.
BenchPerson.objects.filter(pk__in=BenchPerson.objects
                           .filter(addresses__city="city0").values("pk"))
```

### The one manual lever

The fence's crossover depends on how many rows your subquery returns, which the
library cannot see when it compiles a lookup and will not go looking for — every
optimisation here is decided by query shape alone, because SQL that depends on
database state is SQL you cannot read off the code. So the per-query decision is
yours to make, and there is a lookup for it:

```python
BenchPerson.objects.filter(pk__overlay_fenced_in=inner)   # forces = ANY (ARRAY(…))
BenchPerson.objects.filter(pk__in=inner)                  # leaves a plain IN
```

Swept at 1,000,000 people, the same subquery compiled both ways:

| rows the subquery returns | plain `IN` | `ARRAY` | |
|---|---|---|---|
| 200 | 113ms | 8ms | **×15** |
| 5,000 | 177ms | 71ms | ×2.5 |
| 50,000 | 688ms | 600ms | ×1.1 |
| 500,000 | 2,408ms | 2,455ms | ×1.0 |

So it stops paying rather than turning into a penalty on this shape: reach for it
when you know the scope is selective, leave `pk__in` alone when you know it is
not or cannot tell.

The threshold to carry in your head is a **fraction of the table, not a row
count** — measured at both 300,000 and 1,000,000 people, the fence crosses over
somewhere between a scope matching 5% of the table and one matching 50%:

| scope, as a share of the table | at 300,000 people | at 1,000,000 |
|---|---|---|
| ~0.02% | ×70.7 | ×11–13 |
| 0.5% | ×7.0 | ×5.2 |
| 5% | ×2.8 | ×1.8 |
| 50% | ×0.9 | ×0.5 |

Which is why no static row count would have been the right default: the same
absolute scope is worth fencing in a big table and not worth it in a small one. It resolves on any overlay model and raises `FieldError` on a
plain one, because the resolution lives on the overlay query rather than on the
field.

## The number that governs everything

Not the row count. **The share of rows materialised in the tenant's own table**
rather than showing through from the vendor's.

Every query against the view pays for an anti-join that excludes vendor rows the
tenant has taken over, and the cost of that anti-join tracks the size of the
tenant's table. A tenant who has edited 5% of a 400,000-row vendor table pays
far less than one who has edited half of it, on identical data.

400,000 rows, best-of-7, milliseconds. `plain` is one ordinary indexed table
holding all 400,000 — what you would have written without an overlay.

| | plain | 5% edited | 50% edited |
|---|---|---|---|
| **filtering** | | | |
| point lookup, vendor row | 0.23 | **0.56** | **0.50** |
| point lookup, materialised row | 0.20 | **0.55** | **0.42** |
| `age=42`, fetch all matches | 6.71 | **7.06** | 12.50 |
| `age=42`, count | 0.83 | **2.07** | 6.72 |
| count everything | 8.26 | 15.05 | 21.33 |
| **ordering** | | | |
| `age=42`, order by id, first 50 | 0.69 | **2.55** | 8.08 |
| unfiltered, order by id, first 50 | 0.60 | 24.41 | 26.18 |
| **pagination** | | | |
| `CursorPagination`, `age=42` | 1.16 | **3.51** | 7.69 |
| `PageNumberPagination`, `age=42`, page 1 | 1.82 | **5.38** | 14.42 |
| `PageNumberPagination`, `age=42`, deep page | 2.26 | **4.78** | 16.92 |
| `CursorPagination`, no filter | 0.47 | 11.84 | 24.28 |
| `PageNumberPagination`, no filter, page 1 | 8.20 | 39.61 | 47.40 |
| `PageNumberPagination`, no filter, page 200 | 7.99 | 45.35 | 51.09 |

Read across the two right-hand columns rather than down them. At **5%**,
everything with a selective filter is within a few milliseconds of a plain
table, and a point lookup is indistinguishable from one. At **50%** the same
queries cost two to three times as much — still single-digit to mid-teens
milliseconds, but no longer free.

What does *not* improve at either ratio is anything unfiltered. Ordering the
whole table costs ~25ms whether 5% or 50% is materialised, because both branches
have to be read either way.

Three practical consequences:

- **A tenant who edits a lot gets slower for everyone reading that tenant.** The
  anti-join is per-query, not per-row-touched. If a tenant materialises most of
  the vendor table, consider whether they should still be an overlay at all
  rather than a plain copy.
- **`count()` is the sleeper cost.** An unfiltered count is 15–21ms and is what
  makes `PageNumberPagination` expensive on an unfiltered list. Filtered, it is
  2–7ms.
- **Point lookups never degrade.** Detail views, FK traversal and `get_object()`
  stay at half a millisecond regardless of ratio.

## Joins

Each overlay model in a join brings its own anti-join, so the ratio effect
compounds. 200,000 people and 200,000 addresses linked one-to-one through a
plain through table, filtering on a city matching 400 of them:

| query | plain | 5% edited | 50% edited |
|---|---|---|---|
| join filter, fetch matches | 3.18 | **12.39** | 23.75 |
| join filter, first 50 ordered | 1.32 | **11.68** | 21.49 |
| join filter, count | 0.91 | **10.85** | 21.95 |
| reverse join | 6.45 | **12.58** | 25.82 |

A view-to-view join is the most expensive ordinary thing you can do: roughly
2–20× a plain three-table join, 11–26ms absolute, and it doubles from 5% to 50%
because *both* sides' anti-joins grow. Joining an overlay model to a plain table
is about half of that.

`tests/test_joins.py` pins that traversal is **correct** across every shape —
forward and reverse FK, both M2M directions, overlay-to-overlay, multi-hop,
`exclude()` — with the matching row placed in the vendor table each time, since
a join that silently returned only the materialised half would still return rows
and still look right.

Index the join columns on the vendor tables as well as your own. Same rule as
anywhere, easy to forget for a table you don't own.

## Writes

Unaffected by the ratio, and the one place the overlay is simply not slower.
400,000 rows, best-of-7:

| | overlay | plain table |
|---|---|---|
| insert one row (through the `INSTEAD OF` trigger) | **0.2–0.5ms** | 0.1–0.3ms |

Bulk deletes are the exception; see the bottom of this page.

## The indexes the source table needs

Two of them, and neither is the one you would guess. Without them the numbers in
the third column collapse to the second.

`NEGATIVE_ID` gives a source row the id `-source.id`, so `WHERE id = -300000` on
the view reaches the source as `-source.id = -300000`. **No plain index on
`source.id` can serve that** — Postgres won't rewrite the negation. It needs an
index on the negated expression:

```sql
CREATE INDEX source_neg_id ON your_source_table ((-id));
```

Measured on a point lookup: 39ms without it, 0.03ms with it.

The same applies to any composite index you add for filtering and sorting. An
`(age, id)` index on the source is in the wrong order to feed a merge, because
the view sorts by `-source.id`:

```sql
CREATE INDEX source_age_neg_id ON your_source_table (age, (-id));
```

The UUID strategies don't negate anything, so they need ordinary indexes.
`python manage.py show_source_indexes` lists what the source already has.

Beyond that, the rules are the ordinary ones: index every column an
`OverlayUniqueConstraint` covers on **both** tables — the source-side trigger
queries the source on every insert and update, and without an index that is a
full scan per write — and `ANALYZE` the base table after bulk materialising, so
the planner costs the anti-join against real statistics.

## Why the view is written with `NOT EXISTS`

The source branch has to exclude rows the base table has taken over. Expressed
as `NOT IN (SELECT id FROM base)`, Postgres hashes every base row before it can
rule out a single source row — a point lookup measured **39ms**. Expressed as
`NOT EXISTS`, it becomes a real anti-join that can use the base table's primary
key: **0.03ms** with the expression index in place, 4.5ms without.

The two are equivalent here because the base primary key is never NULL, which is
the only case where `NOT IN` and `NOT EXISTS` differ.

## Why ordering is the expensive part

The source branch has to exclude rows the base table has taken over. Postgres
has two ways to run that anti-join:

- **Nested loop** — probe the base table's primary key once per candidate row.
  Cheap when few rows qualify, and it *preserves the order* the source index
  produced.
- **Hash** — scan the whole base table into a hash. Much better in bulk, and it
  *destroys ordering*.

It picks hash as soon as more than a handful of rows qualify, and then the plan
has to `Sort` rather than `Merge Append`:

```
Sort  (Sort Key: ((- personsource.id)))     <- not a Merge Append
  -> Append
      -> Hash Right Anti Join
           -> Seq Scan on person            <- every materialised row, every query
```

A `Sort` can't stop early, so `LIMIT 50` doesn't save any work: both branches
are read in full first. That is the whole story for ordered queries —

- **why a filtered page costs what it costs**: the hash build scans your base
  table, so the price is set by how much you have materialised;
- **why `OFFSET 100000` is worse**: 100,050 rows have to be produced and thrown
  away, with no early exit anywhere;
- **why a point lookup is fast**: one candidate row means a nested loop and an
  index probe, which is why the expression index above takes it to 0.03ms;
- **why keyset pagination only half works**: `id > cursor` helps when it leaves
  few rows qualifying, and does nothing when it doesn't. A cursor into the
  middle of an unfiltered table still leaves half of it to sort.

Adding a composite index on `(filter_col, (-id))` to the source does not change
this. Measured in isolation and in both orders: 7.7ms without it, 7.6ms with it,
and byte-for-byte the same plan. The index feeds the scan correctly and the hash
anti-join above it discards the order anyway. `VACUUM` doesn't help either; it
was measured too.

### Is there a trick for ordering, the way indexes are the trick for filtering?

No. Filtering works because a filter is a *predicate*: Postgres pushes it into
each branch of the `UNION ALL` independently, each branch answers it from its
own index, and the results concatenate. Order isn't a predicate — it's a
property of the whole result — so it needs the two branches merged, not
concatenated.

Postgres has the machinery for that (`Merge Append`) and is perfectly willing to
use it. Four queries over the same two tables, `ORDER BY id LIMIT 50`, differing
by one ingredient each:

| query | plan | time |
|---|---|---|
| `UNION ALL`, plain columns, no anti-join | Merge Append | **0.031ms** |
| `UNION ALL`, negated column, no anti-join | Merge Append | **0.156ms** |
| `UNION ALL` + anti-join | Merge Append, Hash Anti Join | 53ms |
| the real view | Append, Hash Anti Join, Sort | 85ms |

So it is not the `UNION ALL`, and it is **not the negation** — a merge over a
negated column is still sub-millisecond. It is the anti-join, which is the one
thing the overlay can't do without: drop it and every materialised row appears
twice, once from each branch.

Things that were tried and don't fix it:

- **Forcing a merge anti-join** (`SET enable_hashjoin = off`) does work — the
  plan really does become a `Merge Anti Join`, and unfiltered ordering went from
  26ms to 16ms. But the `Sort` survives, so `LIMIT` still can't stop early, and
  every other query got worse. Not worth a global setting.
- **A partial index on the base table** (`(id) WHERE NOT _overlay_deleted`) to
  give the base branch a sorted path: the planner keeps choosing a seq scan, and
  `Merge Append` still doesn't appear. There is a reason for that, and it turns
  out to be a *second* blocker rather than a failure of the index — see below.
- **Switching id strategy.** UUID4 has no negation at all and behaves
  identically, which is what ruled the negation out as the cause.

The lever that does exist is the one already stated: keep the filter selective,
and keep the base table small. Both shrink the anti-join, which is the thing
being paid for.

## What does unlock O(limit) paging

The section above is measured against the default model shape — overridable,
soft-deletable. Two model-level flags change it, and between them they are the
difference between reading every row and reading twenty.

There are **two** blockers, not one, and they are the two anti-join inputs.

**The anti-join**, as described above. `overridable = False` removes it, or
narrows it to tombstones under soft delete. Once it is gone the planner does
choose a `Merge Append` — but only half the plan improves:

```
Merge Append
  ->  Sort (top-N heapsort)   <- base branch, 600,000 rows scanned
  ->  Index Scan Backward     <- source branch, 4 buffers
```

**The soft-delete qual on the base branch** is the second. `WHERE NOT
_overlay_deleted` is what stops that side supplying ordered output, and no index
shape recovers it:

| | base alone | source alone | union of both |
|---|---|---|---|
| no extra index | 0.2ms index scan | 0.2ms index scan | 53.3ms seq scan + sort |
| partial `(score DESC) WHERE NOT _overlay_deleted` | 0.2ms | — | 52.5ms seq scan + sort |
| plain `(score DESC)` | 0.2ms | — | 51.7ms seq scan + sort |
| covering `(score DESC) INCLUDE (id)` | 0.3ms | 0.2ms | 53.1ms seq scan + sort |

Each branch is ordered and instant on its own. The union is not, at any index
shape. Removing the qual is what fixes it:

| | time | plan |
|---|---|---|
| base branch unfiltered | **0.2ms** | `Merge Append`, two ordered scans |
| base branch `WHERE NOT _overlay_deleted` | 55.4ms | seq scan + sort |
| + composite `(_overlay_deleted, score DESC)` | 55.0ms | seq scan + sort |
| + qual rewritten `_overlay_deleted = FALSE` | 56.7ms | seq scan + sort |

With both gone — `overridable = False` **and** `soft_delete = False` — the plan
reads 27 buffers and executes in **0.029ms on 1.2M rows**, and `OFFSET 100000`
only rises to 9.4ms. That is O(limit): the same twenty rows are examined whether
the tables hold 900 thousand or 300 million.

It costs semantics, and the cost is not small. Both anti-join inputs gone means
the tenant can **add** rows and nothing else — no overriding a vendor row, no
deleting one. Use it for append-only link tables, not for entities.

**A qual as such is not the problem.** With the anti-join gone and a *selective*
qual served by a composite index present on **both** branches, the ordered path
survives:

| query | before the index | after `(city, score DESC)` on both branches |
|---|---|---|
| `WHERE city=… ORDER BY score DESC LIMIT 20` | 62.8ms | **0.3ms** |

So the rule is: the ordered path survives when each branch's access is served by
an index supplying **both** restriction and ordering, and the restriction is
selective. A near-universal boolean like soft-delete is not, and defeats it.

### What this means for a real schema

The production-shaped benchmark (`benchmark/`, the `shapes` suite) puts both shapes
on either side of every join — four overridable, soft-deletable entities linked
by three append-only M2M through models:

| link tables (`overridable=False, soft_delete=False`) | overlay | ratio | plan |
|---|---|---|---|
| person_id lookup | 0.2ms | 1.6× | append |
| unscoped ordered page | 0.1ms | **1.1×** | **Merge Append** |
| deep offset | 7.9ms | 1.1× | Merge Append |

| entities (`overridable, soft_delete`) | overlay | ratio |
|---|---|---|
| point lookup by pk | 0.3ms | 2.2× |
| equality, indexed | 0.6ms | 1.5× |
| equality, **unindexed** | 8.3ms | 1.2× |
| scoped + ordered | 0.5ms | 1.9× |
| **unscoped ordered page** | 25.4ms | **97.7×** |

One cliff, and it is the only one. **Scope your list screens** — a single
equality filter on an indexed column took the worst case from 374ms to 3.1ms —
and mirror the composite `(scope, sort)` index onto the source table, or you
lose the `Merge Append` and never find out why.

## Pagination, and DRF's paginators

`tests/probe_ratio.py` issues the query shapes DRF's three built-in paginators
produce. DRF isn't a dependency, but these are what the database sees either
way:

| paginator | queries per page |
|---|---|
| `PageNumberPagination` | `qs.count()` then `qs.order_by(pk)[offset:offset+50]` |
| `LimitOffsetPagination` | the same two |
| `CursorPagination` | `qs.order_by(pk).filter(pk__gt=cursor)[:51]` |

The measured cost of each is in the table above. All three work unchanged, and
none needs special handling. What decides whether a page costs 3ms or 45ms is whether the queryset
carries a selective filter:

- **With a filter, any paginator is fine.** `PageNumberPagination` costs a few
  milliseconds more than `CursorPagination` because it also runs `count()`, and
  the deep page costs barely more than the first — the `Sort` is the price, and
  `OFFSET` on top of it is nearly free by comparison.
- **Without a filter, no paginator saves you.** Keyset is *not* a rescue here:
  a cursor with no filter still leaves most of the table qualifying, so the plan
  is the same hash anti-join and the same non-terminating `Sort`. It measured
  12–24ms — better than `PageNumberPagination`'s 40–51ms only because it skips
  the `count()`.

`CursorPagination` needs `ordering` set to something total and stable; `"id"` or
`"-id"` is the obvious choice, and under `NEGATIVE_ID` it is a valid total order
— just not the one you might expect, since vendor rows carry negative ids and
therefore sort ahead of materialised ones. That divergence is recorded in
[COMPATIBILITY.md](../reference/COMPATIBILITY.md). The UUID strategies don't
negate anything, so their ordering reads normally.

The practical rule the numbers support: **filter selectively.** Keyset
pagination then saves you the `count()` and the `OFFSET` on top, which is worth
having but is the smaller effect. Sorting the whole table is the one thing this
design genuinely cannot do at a plain table's speed.

## Bulk deletes

`queryset.delete()` runs the `INSTEAD OF DELETE` trigger once per row. Deleting
200,000 rows through the view takes minutes; the same rows go in seconds through
`TRUNCATE` or a `DELETE` against the base table. Reads and single-row writes are
not affected — this is specifically the cost of deleting a large set through the
view, and worth routing around when you know you are doing it.
