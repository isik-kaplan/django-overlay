# Query rewriting

django-overlay rewrites three query shapes before they reach the database. All
three are on by default, each has a setting to turn it off, and none of them
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
| `filter(m2m__field=…)` | the same join, plus a redundant fence | `DJANGO_OVERLAY_REWRITE_TRAVERSALS` |
| `select_related('fk')` | `prefetch_related('fk')` | `DJANGO_OVERLAY_REDIRECT_SELECT_RELATED` |
| `fk__in=<queryset>` | `fk = ANY (ARRAY(<subquery>))` | `DJANGO_OVERLAY_ARRAY_SUBQUERY_IN` |

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

## What is *not* rewritten: the unscoped ordered page

```python
BenchPerson.objects.order_by("-score")[:20]     # no filter
```

This is the one shape with no rewrite, and on an entity-shaped model it measured
**97×** a plain table. See
[operations/PERFORMANCE.md](../operations/PERFORMANCE.md#what-does-unlock-olimit-paging)
for why, and for the two model-level flags that remove it.

The short version: scope your list screens. A single equality filter on an
indexed column took the worst case from 374ms to 3.1ms.
