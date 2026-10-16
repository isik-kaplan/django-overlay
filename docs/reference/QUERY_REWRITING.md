# Query rewriting

django-overlay rewrites five query shapes before they reach the database. All
five are on by default, each has a setting to turn it off, and none of them
changes which rows you get back.

They exist for one reason. A `UNION ALL` view is an **appendrel**, and an
appendrel parent carries no statistics: `examine_simple_variable()` has arms for
`RTE_RELATION` and for `RTE_SUBQUERY && !rte->inh`, and a pulled-up `UNION ALL`
is neither. So the planner falls back to `DEFAULT_NUM_DISTINCT` and estimates a
join between two views at 1/200. With a `LIMIT` on top the tuple fraction
collapses, `add_path()` selects on startup cost alone, and the winner is a
nested loop that never terminates early.

Measured on the production-shaped graph, a join between two overlay views ran
76×–1304× a plain table holding identical rows. Every rewrite below is a way of
not making that join, or of giving the planner something it can cost.

| what you write | what runs | setting |
|---|---|---|
| `filter(fk__field=…)` | `filter(fk_id__in=<subquery>)` | `DJANGO_OVERLAY_REWRITE_TRAVERSALS` |
| `filter(m2m__field=…)` | the same join, plus a redundant fence | `DJANGO_OVERLAY_M2M_FENCE` |
| `select_related('fk')` | `prefetch_related('fk')` | `DJANGO_OVERLAY_REDIRECT_SELECT_RELATED` |
| `fk__in=<queryset>` | `fk = ANY (ARRAY(<subquery>))` | `DJANGO_OVERLAY_ARRAY_SUBQUERY_IN` |
| `pk__in=[k1, k2, …]` | `pk = ANY (%s::uuid[])`, one parameter | `DJANGO_OVERLAY_ARRAY_LITERAL_IN` |

`DJANGO_OVERLAY_REWRITE_TRAVERSALS` gates the first two together — it decides
whether a traversal is examined at all — and `DJANGO_OVERLAY_M2M_FENCE` then
decides whether the m2m half of that gets its fence. The fence used to share
`ARRAY_SUBQUERY_IN` with the foreign-key rewrite, so turning that off to spare
one broad `fk__in` also unfenced every m2m traversal in the project.

**One of these is not really optional.** Turning the m2m fence off is not a
performance trade at scale: measured at 1,000,000 people, an unfenced
`filter(pk__in=<m2m traversal>)` plans a Parallel Hash over 7,950,000,000
estimated rows for a 200-row answer, that node's tuplestore lives in dynamic
shared memory, and the backend is killed with signal 9 — taking the instance
into recovery, not just the query. See [operations/PERFORMANCE.md](../operations/PERFORMANCE.md#never).

---

## 1. Forward FK traversals become subqueries

```python
WideOrder.objects.filter(customer__city="Leeds")[:50]
```

becomes `filter(customer_id__in=<subquery>)`, which compiles to
`= ANY (ARRAY(…))` — an InitPlan, evaluated once, so the outer query sees an
array rather than a relation whose size it cannot guess.

**6,195ms → 5.1ms** at 900,000 view rows. Three views deep, 4,989ms → 5.2ms.

A join and a semi-join are only interchangeable when the join cannot multiply
rows and cannot be negated into different NULL semantics, so this is gated hard.
It is skipped for `exclude()`/`~Q()`, for reverse and m2m paths, for expression
values (`F()`, `OuterRef`, `Subquery`), for lookups on the relation itself
(`customer__isnull`, `customer__in`), for paths ending at the target's primary
key, and when the target is a plain table (measured 1.2–1.3× — the plain side's
statistics rescue the estimate and there is nothing to fence).

## 2. M2M traversals get a fence, not a replacement

```python
BenchPerson.objects.filter(phones__number="+447000000042")
```

The rewrite above **cannot** be reused here, and this is the important part: a
semi-join does not multiply rows. A person with three matching phones must
appear three times, and replacing the join with `pk IN (…)` returned 6 rows
where the join returned 10.

So nothing is replaced. An extra condition is `AND`ed on:

```sql
pk = ANY (ARRAY(SELECT person FROM through WHERE phone = ANY (ARRAY(…))))
```

which is **implied by the join Django already emitted** — if a row satisfies the
join, some through row links it to a matching target, so its pk is necessarily
in that set. A conjunct implied by an existing conjunct cannot change which rows
match or how many times each appears. It only hands the planner an InitPlan
where it previously had a blind estimate.

Measured through the ORM on 300,000 people, rows identical in every case:

| query | join only | fenced | |
|---|---|---|---|
| `phones__number=` | 173.5ms | 2.8ms | **61.8×** |
| `phones__kind=` | 6,179.4ms | 733.6ms | 8.4× |
| `addresses__city=` | 3,079.5ms | 34.3ms | **89.8×** |
| `addresses__postcode=` | 728.4ms | 1.8ms | **413.3×** |

Skipped under negation, where the argument collapses — `NOT (A AND B)` is not
`NOT A` — and inside the inner query Django builds for `split_exclude()`, whose
`trim_start()` indexes into the alias map by position.

## 3. `select_related()` becomes `prefetch_related()`

```python
BenchPersonPhone.objects.select_related("person", "phone")
```

`select_related` compiles to a join between two views. `prefetch_related` issues
a second query with a literal id list, which has real statistics and does plain
index lookups — and never joins two views at all.

**289×–302×** on the production-shaped graph, rows identical.

Nothing about attribute access changes. `link.person` is still a single
instance, populated from a cache instead of a join, and still `None` where the
FK is null. Accessing it costs no further query.

What does change:

- **one extra query per redirected path** — that is the trade
- **two snapshots rather than one.** Inside a transaction, or against
  read-mostly vendor data, this is not observable; under `READ COMMITTED` a row
  could change between the two queries.

Plain (non-overlay) targets still join, because their statistics make the join
the right plan. `select_related(None)` still clears.

Refused rather than silently degraded:

- **`.iterator()` without `chunk_size`** — prefetching is skipped entirely, so
  you would get N+1 queries where `select_related` gave you one. The only case
  where routing would make things worse.
- **combinator querysets** (`.union()`, `.intersection()`, `.difference()`) —
  prefetching is not supported after them.

Left alone, because `select_related` is already a no-op there and the join comes
from the path itself: `.values()`/`.values_list()`, `order_by('person__city')`,
`annotate(…'person__…')`.

---

## 4. A literal key list is sent as one parameter

```python
keys = list(scope.values_list("pk", flat=True))
BenchPerson.objects.filter(pk__in=keys)
```

The plan does not change — Postgres turns `IN (a, b, …)` into `= ANY` of a
constant array before planning — but the way there does. Django prepares an
`IN` list one value at a time, twice, gives each its own placeholder, and
psycopg2 adapts each on its own; on a uuid key that is several Python calls per
value. The library makes these lists itself — a `select_related()` redirected
to a prefetch sends every parent key — and code that reads a scope's keys and
filters by them makes them too. So a list on a key column, whose values are
already the column's type (`uuid.UUID`, or `int` for an integer key), compiles
to `pk = ANY (%s::uuid[])` with the whole list as one text parameter.

`django-overlay benchmark --suite lists`, 1,000,000 people, timed from reading
the keys to having the rows, rows identical in every column:

| keys | IN list | one array | kept in SQL | plain, IN list |
|---|---|---|---|---|
| 1,000 | 11ms | 10ms | **4ms** | 6ms |
| 10,000 | 55ms | 36ms | **16ms** | 46ms |
| 100,000 | 567ms | 329ms | **160ms** | 524ms |
| 500,000 | 2,849ms | 1,809ms | **768ms** | 2,628ms |

Left to Django: a single distinct value (Postgres plans `IN (x)` as `= x`, and
this rewrite promises not to change a plan), model instances, strings, mixed
types, a `bool` among integers, an expression on the left, and anything that is
not a list, tuple or set.

**The better fix is not to make the list.** The "kept in SQL" column is the
same scope passed as a queryset — `filter(pk__overlay_selective_in=scope.values("pk"))`
— which never brings the keys into Python at all, and is two to four times
faster again. Read keys into Python when Python needs them; when the next
query is the only thing that does, hand it the queryset.

## What is *not* rewritten: the unscoped ordered page

```python
BenchPerson.objects.order_by("-score")[:20]     # no filter
```

One of two shapes with no rewrite, and on an entity-shaped model it measured
**97×** a plain table. See
[operations/PERFORMANCE.md](../operations/PERFORMANCE.md#what-does-unlock-olimit-paging)
for why, and for the two model-level flags that remove it.

The short version: scope your list screens. A single equality filter on an
indexed column took the worst case from 374ms to 3.1ms.

## What is *not* rewritten: a reverse foreign key between two views

```python
BenchPerson.objects.filter(benchpersonaddress__address_id__in=ids)
# BenchPersonAddress.person is an OverlayForeignKey, and both models are views
```

This compiles as the plain join. Neither rewrite above fits it: replacing the
join with a semi-join would deduplicate — a person with two matching rows must
come back twice — and the m2m fence only knows link tables. The m2m fence's
conjunct does carry over, `pk = ANY (ARRAY(SELECT person_id FROM related WHERE
…))`, but measured, it is not something to turn on by default.

`django-overlay benchmark --suite reverse`, at 1,000,000 people, rows identical in
every column:

| scope | join | join + fence | semi-join | plain |
|---|---|---|---|---|
| 200 people | 107ms | 15ms | **6ms** | 18ms |
| 5,000 | 235ms | 74ms | **36ms** | 62ms |
| 50,000 | 635ms | 1,068ms | **425ms** | 505ms |
| 500,000 | 839ms | 1,647ms | **702ms** | 390ms |
| 2,267, beside two other array fences | **705ms** | 5,056ms | 5,003ms | >30s |
| 22,667, beside two other array fences | **1,049ms** | >30s | >30s | >30s |

On its own the fence has the usual crossover — seven times faster on a narrow
scope, half as fast on a broad one. Beside other array fences it collapses: the
planner cannot know an `ARRAY(subquery)`'s length, so each array's size is a
default guess, and the guesses multiply until a scope of thousands is estimated
at a handful of rows and a nested loop is chosen for it. The join is never worse
than about a second. A default that is sometimes thirty times slower is not a
default, so there isn't one.

**The lever, when you know the shape.** If duplicates do not matter — a scope,
a count of distinct people, a `pk__in` — write the condition as a semi-join on
the related model and leave the join out:

```python
links = BenchPersonAddress.objects.filter(address_id__in=ids).values("person_id")
BenchPerson.objects.filter(pk__overlay_selective_in=links)
```

That is the semi-join column: faster than the join at every scope when it stands
alone. Do not combine it with other array fences in the same statement — the
last two rows are what that costs.
