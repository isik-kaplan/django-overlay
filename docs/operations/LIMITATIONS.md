# Left to you

- **Where the source comes from** — `OverlayMeta.get_source()` is a plain
  function; hardcode it, read from your own config model, whatever. Expected
  to resolve to the same table every time for a given tenant once set —
  switching it is a deliberate, explicit resync (see [MIGRATIONS.md](MIGRATIONS.md)),
  not something the package detects on its own.
- **Filtering a source's rows** (e.g. a subscription-tier subset) —
  `SourceTable.extra_where` is the hook; it's raw SQL spliced directly into
  the view's `CREATE VIEW` text (a view has no bind parameters). A hardcoded
  literal you wrote yourself is fine. If the value comes from *data*, quote
  it yourself first (e.g. `psycopg.sql.Literal(value).as_string(connection)`)
  — don't f-string it in.
- **Delete semantics** — see [DELETION.md](../concepts/DELETION.md).
- **Multi-tenancy** — uses `connection.schema_name` if `django_tenants` is
  installed, else Postgres's own `current_schema()`.

`Meta.permissions`/`Meta.default_permissions` aren't supported — declaring
either raises `OverlayConfigurationError`. Neither model is a sound home for
them: the view model is `managed=False` so `create_permissions()` silently
skips it; the hidden base model would create them under a codename tied to
a table application code should never reference directly. The base model's
own default permissions are always suppressed (`default_permissions = ()`).

## When a constraint violation surfaces

The same moments Django gives you, which is worth stating because the
mechanisms differ.

**Foreign keys — at COMMIT.** `OverlayForeignKey` is enforced by a
`DEFERRABLE INITIALLY DEFERRED` constraint trigger, because Postgres can't hold
a real foreign key against a view. That is exactly what Django emits for its
own foreign keys on PostgreSQL, so the timing is identical to a plain project:

```python
with transaction.atomic():
    try:
        Note.objects.create(person_id=bogus)
    except IntegrityError:
        ...          # not reached -- and not reached with a plain FK either
# IntegrityError lands on the outermost COMMIT
```

To check early, in either case:

```python
with connection.cursor() as cursor:
    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
```

**Uniqueness — at the statement.** The base-table half is a native unique
index, and the source-side trigger is `DEFERRABLE INITIALLY IMMEDIATE` to
match, so both halves of one `OverlayUniqueConstraint` fail at the same moment.
`SET CONSTRAINTS ALL DEFERRED` still defers the source-side half if you want
that.

`tests/test_constraint_timing.py` pins all of this against Django's own
constraints, so a Django change that moves the goalposts fails the suite.

## Uniqueness must be declared as `OverlayUniqueConstraint`

`Meta.unique_together`, `Field(unique=True)` and plain `UniqueConstraint` are
rejected at import time on an overlay model, because none of them can cover the
source table. See [UNIQUENESS.md](../concepts/UNIQUENESS.md).

## `bulk_create()` conflict handling

`ignore_conflicts=True` and `update_conflicts=True` raise
`OverlayConfigurationError`. Django puts `ON CONFLICT` on the relation it
inserts into — the view — which has no unique index for it to match, while the
real insert happens a level down inside the trigger. Plain `bulk_create()` is
unaffected and still returns primary keys.

## Concurrency: no row locking

Both of these come from the same cause — the model is a view with `INSTEAD OF`
triggers, and Postgres will not lock a view's rows.

**`select_for_update()` raises.** Postgres accepts `SELECT ... FOR UPDATE`
against such a view and marks no rows, so it would look like mutual exclusion
and provide none. Rather than let that pass silently, django_overlay refuses it.

**`F()` expressions are atomic, but by a different route.** An expression that
reads a column of the row it updates can't survive the view — it gets folded
into a literal before the trigger sees it — so django_overlay spots that shape,
copies the matched rows into the base table, and applies the update there,
where Postgres's ordinary row locking does the work. You write plain Django;
`update(age=F("age") + 1)` is atomic. Everything else keeps the single-statement
path.

This covers all three ways an `F()` reaches the database — `update()`,
`obj.save()`, and `bulk_update()` — not just `update()`. There is no `OverlayF`
to remember.

A read-modify-write done in Python — read, compute, `save()` — is
last-writer-wins, exactly as in Django. For a longer critical section, take an
advisory lock on the row for the duration:

```python
TABLE_KEY = 4021  # any stable int per table

with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [TABLE_KEY, person_id])
    person = Person.objects.get(pk=person_id)
    person.age += 1
    person.save()
```

The lock is held to the end of the transaction and is released automatically.
`tests/test_select_for_update.py` proves this serialises where
`select_for_update()` did not.

Neither limitation involves the source table — a source-less overlay model
behaves the same way.

## Ordering by id is not insertion order under `NEGATIVE_ID`

The one divergence that neither raises nor is caught by a check, so it is worth
knowing about explicitly.

`NEGATIVE_ID` gives a source row the id `-source.id`, which keeps the mapping
reversible and collision-free but means source-backed rows sort *before* every
organic row, and among themselves in reverse insertion order. On a real table
`order_by("id")` is a rough proxy for "oldest first"; here it is not.

Nothing is lost or wrong — the rows and their contents are exactly right, only
the sequence differs — but code that leans on `id` ordering to mean *time* will
read differently than it did against a real table. Order by a timestamp column
if you mean chronology, or use one of the UUID7 strategies, whose ids are
time-ordered by construction (see [IDS.md](../concepts/IDS.md)).

## Referential integrity

Postgres can't hold a real foreign key against a view, so `OverlayForeignKey`
is enforced by two deferred constraint triggers instead — one on each side,
which is how Postgres implements real foreign keys too:

- **Insert side**, on the referencing table: refuses a reference to a row that
  isn't visible through the target's view (its base table or its source).
- **Delete side**, on the target's base table: refuses removing a row that is
  still referenced. It asks whether the identity is still visible through the
  view rather than whether a row was deleted, so a hard delete, a soft delete
  and `reset_to_source()` are all handled by the same check — and
  `reset_to_source()` on a source-backed row is correctly allowed, because the
  source row shows through and the identity survives.

Both fire for **any** write, not just ORM ones, so raw SQL, a data migration or
another service on the same database can't strand a reference either.

Two things to know:

- **`on_delete=DO_NOTHING` raises** if the target is still referenced. That is
  what Django documents for `DO_NOTHING` against a backend that enforces
  integrity. `CASCADE`, `PROTECT` and `SET_NULL` are unaffected — the collector
  deals with the children before the parent row goes.
- **A source row deleted by its owner is out of reach.** We don't own that
  table and can't put a trigger on it, so if the vendor deletes a row your
  references to it dangle. Nothing in this design can prevent that.
- **The same is true of the source changing wholesale.** Every check above
  fires on a write to *your* table, so replacing the source table underneath
  them can invalidate all of them without a single trigger firing. Doing that
  deliberately is [SOURCE_SWAPS.md](SOURCE_SWAPS.md).
