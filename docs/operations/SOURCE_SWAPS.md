# Swapping the source table

A blue-green deployment of the table underneath an overlay model: build the new
one alongside the old, verify it while the old one is still serving, and cut
over in a single transaction.

## Why this is not a configuration change

Every integrity guarantee this package makes is a row trigger on a table
*you* own. `OverlayUniqueConstraint`'s source-side half fires when you insert
into the base table. `OverlayForeignKey`'s insert side fires when you write the
reference; its delete side fires when the target's base row goes. Not one of
them can fire for a write to the source table, because the source table is
somebody else's and carries no trigger of ours.

Replacing it is millions of rows appearing and disappearing with no event any
of those triggers can see. Two of the things that lets through are silent and
permanent:

**Renumbering.** The source's id is the overlay's identity — the view's primary
key (negated, under `NEGATIVE_ID`), the value a materialised row shadows, the
value a tombstone masks, and the value in every `OverlayForeignKey` column
pointing at the model. A new table that hands the same ids to different
entities leaves every one of those resolving perfectly, to the wrong rows.
Nothing raises, now or ever.

**Late collisions.** A value the source did not hold when you wrote yours, and
holds now, is a uniqueness violation that no index and no trigger will report.
The view simply starts returning two rows for a column you declared unique.

So a swap is a procedure. `django_overlay.swaps` is that procedure.

## What will not work

**Renaming.** A view's dependency is on the underlying relation's OID, not its
name:

```sql
ALTER TABLE vendor.people RENAME TO people_blue;
ALTER TABLE vendor.people_green RENAME TO people;
```

The view still reads the same physical table, now called `people_blue`. No
cutover happens and nothing says so. The same is true of schema renames. The
only real cutover is `CREATE OR REPLACE VIEW` against the new relation, which
is what `sync.sync_view()` emits and what `swap_source()` runs.

## The procedure

### 1. Build the candidate

Same columns, same types, same ids meaning the same things. Give it every index
the current source has, plus the ones the model's own triggers need
(`manage.py show_source_indexes` names them) — build them before the cutover,
because at the instant the view is replaced every uniqueness and foreign-key
probe starts hitting the new table. `ANALYZE` it. The view is a `UNION ALL`,
which is already the shape Postgres estimates worst; a table with no statistics
under it makes that worse.

### 2. Verify it while the old one is still live

```bash
python manage.py swap_source myapp.Person \
    --candidate-schema vendor --candidate-table people_green \
    --identity-column ssn
```

Nothing is changed. `--identity-column` is the source's natural key — repeat it
for a composite one. Leaving it out is allowed and reported (`S005`), because
the check it skips is the one that matters most.

Or from Python, which is the same thing with the report as an object:

```python
from django_overlay.swaps import verify_source_swap
from django_overlay.sources import SourceTable

report = verify_source_swap(
    Person,
    SourceTable(schema="vendor", table="people_green"),
    identity_columns=["ssn"],
)
print(report)
report.ok        # False if anything blocking was found
report.errors    # what blocks
report.warnings  # what does not
```

### 3. Flip `get_source()`, then cut over

```bash
python manage.py swap_source myapp.Person --identity-column ssn
```

With no `--candidate-*`, the configured source is the candidate and the
*deployed* one — read back out of the view's catalogue entry, not out of
config — is what it is checked against. Config first and cutover second is the
ordering that leaves nothing to revert: the two agree the moment the command
returns, so the next unrelated `resync_overlay_views` rebuilds what was just
deployed instead of quietly putting the old source back.

`--dry-run` runs the whole preflight against the configured source and stops.

## What the cutover does

One transaction:

1. `EXCLUSIVE` on the base table. Every write to an overlay model lands there
   in the end, including writes that never went through the ORM, so this
   freezes exactly what the row-level checks are about — and it blocks no
   readers. (Locking the *view* would be the obvious move and the wrong one:
   Postgres locks the relations named in a view's definition along with it, so
   that reaches through to the vendor's table and blocks every reader of it
   everywhere.)
2. The row-level checks again. The preflight ran minutes ago and has not seen
   what landed since — and a deferred foreign-key trigger validates against the
   source at the moment it fires, not the moment it commits.
3. `CREATE OR REPLACE VIEW`, the three `INSTEAD OF` triggers, every uniqueness
   trigger, and the insert and delete sides of every foreign key pointing at
   this model.

Postgres does DDL transactionally, so other sessions see the old arrangement or
the new one — never a view reading one table while the constraints guarding it
probe another. `lock_timeout` bounds the wait: better to fail and retry than to
queue behind a long read and hold everything behind you.

Rebuilding the constraint triggers is the part that is easy to miss, and it is
why this cannot be done by hand with a `CREATE OR REPLACE VIEW`. Those bodies
name the source table as literal PL/pgSQL text, and the insert side of a
foreign key lives on the *referencing* table — so it is not visible from the
model being swapped at all. `resync_overlay_views` rebuilds all of them too.

## What it checks

| Code | Level | What it means |
| --- | --- | --- |
| `S001` | error | A table does not exist. Nothing else can run. |
| `S002` | error | A column the view selects is missing, or reads back as a different type. |
| `S003` | error | An id carries a different natural key than it does today. **The one that corrupts silently.** |
| `S004` | error | A row kept its natural key and changed id. References to it dangle. |
| `S005` | warning | No `identity_columns` given, so `S003`/`S004` did not run. |
| `S006` | warning | Base rows lose their source row. They stay visible and stop being vendor-backed. |
| `S007` | error / warning | References that would dangle. A warning if they already dangle today. |
| `S008` | error / warning | A constrained value appears twice within the candidate. A warning if it does today too. |
| `S009` | error | The candidate holds a value a base row already holds. |
| `S010` | warning | The candidate is missing an index the current source has. |
| `S011` | warning | A relation or uniqueness column has no index on the candidate. |
| `S012` | warning | The `partition_key` declaration no longer describes the table. |
| `S013` | error / warning | The candidate is empty, or much smaller than the current source. |
| `S014` | error | `extra_where` does not resolve against the candidate. |
| `S015` | warning | The candidate has never been analysed. |
| `S016` | error | `identity_columns` names a field the model does not have. |
| `S017` | error | No single source relation could be read out of the view. |
| `S018` | warning | The view already reads the candidate. Nothing to do. |

Only findings that mean *silent* breakage block. Anything can be accepted
deliberately:

```bash
python manage.py swap_source myapp.Person --identity-column ssn --allow S006
```

An allowed finding is downgraded, not hidden — it still appears in the report.

## After the cutover

**Do not drop the old table yet.** Two reasons, and the second one expires.

Rolling back is the same operation pointed the other way: put `get_source()`
back and run the command again. It is not free — rows written since the cutover
were validated against the new source, so the reverse direction gets the same
preflight and can be refused — so keep the window short. But it exists only
while the old table does.

The second reason is the one you cannot get back. Materialisation copies whole
rows, so there is no per-column record of what a tenant actually edited. A base
row diffed against the source row it was copied from *is* that record, and the
old table is the only place that diff can be computed. If you want overridden
rows to pick up the new source's refreshed values in the columns nobody
touched, this window is when it is possible.

Then re-run the ordinary checks against the new arrangement:

```bash
python manage.py check --database default
python manage.py show_source_indexes
```

## Multi-tenancy

`get_source()` resolves per tenant and a view is per schema, so a shared source
table backs one view per tenant schema. Each is its own cutover: run the
command per schema (`--database`, or under `django_tenants`' schema context),
and the atomicity guarantee holds per view rather than across all of them.

Where that is too many objects to swap one at a time, the alternative is to
point `SourceTable` at a view *you* own that selects from the vendor's table.
Cutover is then one `CREATE OR REPLACE VIEW` on that one object and no overlay
view or trigger changes at all. It costs you the index and partition checks —
`S010`, `S011` and `S012` read a relation's catalogue entry, and a view has no
indexes and no partitions — so point the preflight at the concrete table even
when the model reads the indirection.

That indirection is also where an identity map goes, if the vendor's id is a
surrogate that gets reassigned on every load. Join their natural key to a
stable id you allocate and never reuse, and `S003` stops being a thing that can
happen rather than a thing that gets caught.

## What is still not covered

A source row the vendor deletes tomorrow. We do not own that table and cannot
put a trigger on it, so a reference to a row they remove dangles, and no swap
procedure changes that — see [LIMITATIONS.md](LIMITATIONS.md). The preflight
reports references that already dangle (`S007`, as a warning) precisely so that
this shows up as the pre-existing condition it is, rather than as something the
swap did.
