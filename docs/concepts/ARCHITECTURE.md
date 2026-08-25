# How it works

`class Person(OverlayModel)` becomes two real Django models:

- a hidden `managed=True` table (`person`) — the only thing ever written to
- `Person` itself, `managed=False`, bound to a view (`person_view`) that's a
  `UNION ALL` of the base table and the model's one declared `SourceTable`
  (if any)

Table naming is automatic, not something you set: the base table is named
after `OverlayMeta.table_name` (defaults to the lowercased class name), and
the view is that name plus `_view`. `Meta.db_table`/`Meta.managed` aren't
levers here — declaring either raises `OverlayConfigurationError` at
class-definition time rather than silently being overwritten (same for a
non-`Strategy` `OverlayMeta.strategy` or a non-`bool` `OverlayMeta.soft_delete`
— both get type-checked there too, since e.g. `soft_delete = "false"` is
truthy and would otherwise silently enable it instead of disabling it).
Every generated view/function/trigger name derived from `table_name` is
quoted, so an unusual choice (starting with a digit, a reserved word, mixed
case) is safe either way.

Three `INSTEAD OF` triggers on the view make it writable:

- **INSERT** — assigns an id, inserts into the base table
- **UPDATE** — updates the base table row if it exists there already,
  otherwise materialises one and then applies the edit (copy-up, in two
  statements — see below)
- **DELETE** — see [DELETION.md](DELETION.md)

## Copy-up happens in two statements

The first edit of a source-backed row writes twice: an `INSERT` carrying the
source's own values, then an `UPDATE` applying the edit. The row it leaves is
identical to what one combined `INSERT` would have produced — that equivalence
is pinned across every ORM shape and both id strategies by
`tests/test_query_shapes.py` and `tests/test_atomic_update.py`.

The split is for anything watching the base table. A row-level `AFTER` trigger
— [django-pghistory](https://django-pghistory.readthedocs.io/), or your own
audit table — sees a materialise carrying the source's values, then a change
touching only the columns the caller actually edited. Collapsed into one
statement, the first edit reports *every* column as written, and there is no
way afterwards to tell an override from a value that merely came across with
it.

That distinction is what "has this tenant overridden this field?" needs, and
that question is what a refresh-from-source has to ask before overwriting
anything. `tests/test_copy_up_shape.py` pins the two-write shape directly,
using exactly such a trigger.

To track it, point the history library at the **base** model, not the overlay
model:

```python
Person.base_table()      # the managed=True model the writes actually land on
```

A row-level `AFTER` trigger cannot be attached to a view — Postgres only
accepts `INSTEAD OF` there — so tracking `Person` itself will not work. Writes
that arrive through the view still fire triggers on the base table, so the
base model sees ORM writes, view writes and raw SQL alike.

Only the first edit of a row pays for the split; once the base row exists the
`UPDATE` matches and the materialise never runs. The bulk path
(`OverlayQuerySet._copy_matched_rows_to_the_base_table()`) has always worked
this way, so a queryset `update()` and a `save()` of the same edit now leave
the same two writes behind rather than different shapes.

`OverlayForeignKey` sets `db_constraint=False` (Postgres can't FK a view)
and instead ships a `CREATE CONSTRAINT TRIGGER`, deferred to commit like a
real FK, checking the referenced id exists in the base table or a source.
Passing `db_constraint` yourself raises `OverlayConfigurationError` — same
reasoning as `Meta.db_table` above, it would just be silently overwritten.

Every statement lives as a `.sql.j2` template under `django_overlay/sql_templates/`
(`view/`, `triggers/`, `ddl/`, `pk_defaults/`, `introspection/`) — `sql.py`
only computes context data (column lists, booleans, table names) and
renders; no SQL text is assembled with Python string formatting anywhere.
