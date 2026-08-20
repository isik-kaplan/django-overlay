# Compatibility reference

What behaves like a plain Django model and what doesn't. Every row is measured
by a probe in `tests/`, named at the end of each table — run it yourself rather
than trusting this page.

Legend: **OK** identical to a plain table · **RAISES** refused loudly, at import
or at the call · **DIFFERS** works, but not the same · **SILENT** works, differs,
and nothing tells you

---

## Summary

| | count |
|---|---|
| OK | 46 ORM behaviours, 13 soft-delete behaviours |
| RAISES | 15 declaration and call shapes |
| DIFFERS by design | `.delete()`, `reset_to_source()` |
| SILENT | 1 — `order_by("id")` under `NEGATIVE_ID` |

---

## Reading and querying

| Behaviour | | Note |
|---|---|---|
| `filter()` / `Q` / `count()` | OK | |
| `get()` / `latest()` / `last()` | OK | |
| `aggregate()` across base + source | OK | |
| `values().annotate()` GROUP BY | OK | |
| `distinct("field")` (DISTINCT ON) | OK | |
| `order_by()` + slicing | OK | |
| `order_by("id")` = insertion order | **SILENT** | source rows sort first under `NEGATIVE_ID` |
| `in_bulk()` | OK | |
| `iterator(chunk_size=…)` | OK | server-side cursor |
| `Exists()` / `Subquery()` / `OuterRef()` | OK | |
| Window functions | OK | |
| `select_related()` across `OverlayForeignKey` | OK | |
| `prefetch_related()` reverse | OK | |
| Filtering across a reverse relation | OK | |
| `QuerySet.union()` | OK | |
| `Model.objects.raw()` | OK | |
| `refresh_from_db()` | OK | |
| `only()` / `defer()` | OK | |
| `QuerySet.explain()` | OK | |
| `select_for_update()` | **RAISES** | a view's rows can't be locked; see below |

`tests/probe_orm_conformance.py` — 46 OK / 0 not OK

## Writing

| Behaviour | | Note |
|---|---|---|
| `create()`, with or without explicit pk | OK | |
| `save()` | OK | |
| `save(update_fields=[…])` on a source-only row | OK | |
| `get_or_create()` / `update_or_create()` | OK | |
| `bulk_create()` | OK | returns pks |
| `bulk_create(ignore_conflicts=True)` | **RAISES** | no unique index on a view to conflict against |
| `bulk_create(update_conflicts=True)` | **RAISES** | same |
| `bulk_update()` | OK | organic and source-only rows |
| `update()` | OK | rowcount matches, including "changed nothing" |
| `update()` on source-only rows | OK | copies the row over, then edits |
| `serialize()` / `deserialize().save()` | OK | dumpdata / loaddata |
| `flush()` / TRUNCATE | OK | views excluded from introspection |

### `F()` expressions

Atomic on all three routes. No `OverlayF` to remember.

| Route | before the fix | now |
|---|---|---|
| `queryset.update(F)` | 98/160 | **160/160** |
| `instance.save()` with `F` | 109/160 | **160/160** |
| `bulk_update()` with `F` | 160/160 | **160/160** |

Four threads × 40 increments of one row. A plain table scores 160/160.

`tests/probe_f_routes.py`, `tests/probe_lost_updates.py`

### Concurrency

| Behaviour | | Note |
|---|---|---|
| `F()` increment | OK | routed around the view |
| Read-modify-write in Python | OK | last-writer-wins, same as Django |
| Two writers, different columns | OK | both survive |
| `select_for_update()` | **RAISES** | Postgres accepts it against the view and locks nothing |
| `pg_advisory_xact_lock` | OK | the supported way to hold a critical section |

## Deleting

Default is `soft_delete = True`.

| Behaviour after `.delete()` | | |
|---|---|---|
| Hidden from `filter()` / `exists()` | OK | |
| Hidden after deleting an untouched source row | OK | |
| `get()` raises `DoesNotExist` | OK | |
| `delete()` returns the right row count | OK | |
| `count()` / `aggregate()` exclude it | OK | |
| `values_list()` / `iterator()` exclude it | OK | |
| Reverse-relation joins exclude it | OK | |
| `on_delete=CASCADE` removes dependents | OK | |
| `select_related()` past a masked row | OK | |
| Tombstone invisible outside the base table | OK | |
| Reusing a unique value afterwards | OK | partial index excludes tombstones |
| `full_clean()` agrees with the database | OK | |
| `reset_to_source()` undoes the mask | OK | pristine source values return |
| **Re-inserting the same pk** | **DIFFERS** | the tombstone still holds it |

`tests/probe_soft_delete_compat.py` — 13 OK / 1 not OK

### The three delete differences

| | What | Why |
|---|---|---|
| pk not freed | `create(pk=<deleted pk>)` raises | uniqueness is a partial index (`WHERE NOT _overlay_deleted`); Postgres has no partial primary key |
| Row still there | invisible via the view, visible to raw SQL on the base table; holds storage and index entries | that's what masking a source row requires |
| `reset_to_source()` | skips `on_delete` entirely | it isn't a delete — the identity usually resolves via the source instead |

### `soft_delete = False`

Delete stops meaning delete for source-backed rows.

| Row | `.delete()` does |
|---|---|
| Organic (no source counterpart) | permanent delete — same as Django |
| Source-backed, materialised | **reverts** to the source's pristine values, discarding edits |
| Source-backed, never touched | no-op |

Worth setting on a purely organic model — with no source row to mask, a
tombstone buys nothing and holds an index entry forever.

## Constraints and validation

| Behaviour | | Note |
|---|---|---|
| `OverlayUniqueConstraint`, local collision | OK | rejected by the database |
| `OverlayUniqueConstraint`, source collision | OK | rejected by the database |
| Same, via `full_clean()` | OK | validation agrees with the database |
| `validate_constraints()` | OK | |
| `Meta.constraints` visible from the queried model | OK | via `get_constraints()` |
| Source rows visible to ORM uniqueness lookups | OK | |
| Uniqueness fails at | statement | matches a native unique index |
| `OverlayForeignKey` violation fails at | COMMIT | matches Django's own FKs on PostgreSQL |

`tests/probe_constraint_kinds.py`, `tests/test_constraint_timing.py`

## Declaring a model

| Shape | | Message points you to |
|---|---|---|
| `OverlayForeignKey` overlay → overlay | OK | |
| Self-referential `OverlayForeignKey` | OK | |
| Plain FK **from** an overlay model | OK | ordinary target, ordinary FK |
| Plain FK **to** an overlay model | **RAISES** `E001` | `OverlayForeignKey` |
| Plain `ManyToManyField` to an overlay model | **RAISES** `E002` | `OverlayManyToManyField` |
| `Field(unique=True)` | **RAISES** `E003` | `OverlayUniqueConstraint` |
| `Meta.unique_together` | **RAISES** `E003` | `OverlayUniqueConstraint` |
| Plain `UniqueConstraint` | **RAISES** `E003` | `OverlayUniqueConstraint` |
| `UniqueConstraint(condition=…)` | **RAISES** `E003` | hand-written `RunSQL` index |
| `Meta.db_table` / `Meta.managed` | **RAISES** | `OverlayMeta.table_name` |
| `Meta.permissions` / `default_permissions` | **RAISES** | neither model is a sound home |
| `OverlayForeignKey(db_constraint=…)` | **RAISES** | always `False`; can't FK a view |
| Missing / wrong-typed `OverlayMeta` | **RAISES** | `get_source()`, `strategy`, `soft_delete` are type-checked |
| Own `_overlay_deleted` field | **RAISES** | reserved name |
| Multi-table inheritance from a concrete overlay model | **RAISES** | not supported |

All of these are hard stops. The rest raise at class-definition time;
`E001`–`E003` are system checks that also run from `AppConfig.ready()`, which
is part of `django.setup()` — so `--skip-checks` can't get a misconfigured model
as far as emitting DDL or serving a request.

`tests/probe_declaration_shapes.py`, `tests/test_checks.py`

---

## Where the SQL differs

You write the same ORM code; the statements underneath differ.

| Operation | Plain model | Overlay model |
|---|---|---|
| `SELECT` | one table | view: base table `WHERE NOT _overlay_deleted` `UNION ALL` source `WHERE id NOT IN (SELECT pk FROM base)` |
| `INSERT` | one statement | one statement → `INSTEAD OF INSERT` trigger assigns the id and writes the base table |
| `UPDATE`, literal values | one statement | one statement → `INSTEAD OF UPDATE` trigger updates the base row, or copies it up from the source first |
| `UPDATE`, self-referencing `F()` | one statement | two, in a transaction: `INSERT … SELECT … ON CONFLICT DO NOTHING` to materialise, then a plain `UPDATE` on the base table |
| `DELETE` | one statement | `soft_delete=True` → `UPDATE … SET _overlay_deleted = TRUE`; else `DELETE` from the base table only |
| Unique constraint | one unique index | partial unique index on the base table **+** a trigger checking the source |
| Foreign key | `REFERENCES` | two deferred `CONSTRAINT TRIGGER`s — insert side on the referencing table, delete side on the target's base table |

Consequences worth knowing:

- The `F()` path skips the `INSTEAD OF UPDATE` trigger, whose only job is
  routing view writes to the base table. Unique and FK triggers live on the
  base table and still fire.
- Raw SQL against the **base table** must supply `_overlay_deleted` — there's
  no database-level default. Writes through the view don't.
- `TRUNCATE` on a base table fails with `cannot TRUNCATE … pending trigger
  events` if deferred FK triggers are queued. `SET CONSTRAINTS ALL IMMEDIATE`
  first.
- The source-side anti-join matches **all** base rows, tombstones included —
  that is what keeps a soft-deleted source row hidden rather than un-masking it.
- Both FK triggers fire for **any** write, not just ORM ones — raw SQL, a data
  migration, or another service can't strand a reference.

## Where the models differ

`class Person(OverlayModel)` becomes two Django models.

| | `Person` (what you use) | `PersonBase` (hidden) |
|---|---|---|
| Bound to | `person_view` | `person` |
| `managed` | `False` | `True` |
| Written by | nothing directly | the `INSTEAD OF` triggers |
| Carries | your fields | your fields + `_overlay_deleted` |
| Migrations | view sync operations | ordinary `CreateModel` etc. |

- `Person._base_manager` is the overlay manager, not a plain `Manager` — it's
  what `save()` writes through, so it has to carry the same routing. Set on
  `_meta`, not in `Meta`, so it doesn't ask every project for an
  `AlterModelOptions` migration.
- `Meta.constraints` is reported through `get_constraints()` rather than
  attached to both models, which would trip Django's `models.E032`.
- `OneToOneField` stays a `OneToOneField` on the view model; the base model's
  copy is retyped to a `ForeignKey` so the reverse accessor isn't claimed twice.

## Cost

Best-of-5, milliseconds. The view costs a trigger call per row.

| rows | INSERT table | INSERT view | × | UPDATE table | UPDATE view | × |
|---|---|---|---|---|---|---|
| 1 | 0.93 | 1.17 | 1.3 | 0.05 | 0.10 | 1.9 |
| 10 | 0.94 | 1.05 | 1.1 | 0.06 | 0.13 | 2.1 |
| 50 | 1.22 | 1.07 | 0.9 | 0.09 | 0.25 | 2.7 |
| 500 | 1.40 | 2.53 | 1.8 | 0.98 | 2.31 | 2.4 |
| 5000 | 5.61 | 12.54 | 2.2 | 50.96 | 86.97 | 1.7 |

Per-row overhead settles at 1–7 µs. At realistic batch sizes:

| Operation | |
|---|---|
| `bulk_create(50)` | 1.28 ms |
| `bulk_update(50)` | 3.16 ms |
| `update()` materialising 50 source rows | 0.30 ms |

`tests/probe_write_cost.py`

---

## Running the probes

```
POSTGRES_USER=postgres uv run pytest tests/probe_orm_conformance.py -s -q --no-cov
```

Probe files aren't collected by the default suite — they measure and report
rather than assert, so they belong in a report, not a gate. The behaviours they
cover are pinned by ordinary tests alongside them.
