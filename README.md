# django-overlay

A writable Django model layered on top of a read-only table you don't own —
one model to query, no `COALESCE`/`FULL OUTER JOIN` at read time.

## The problem

You have a big shared read-only table (a vendor import, a third-party dataset)
and a small table of tenant edits on top of it. You want:

- one Django model to query, with normal filters and indexes
- writes to only ever land in your own table, never the shared one
- an edit to copy the full row over the first time it's touched, so reads
  never merge two tables at query time
- a foreign key can still point at it, even though Postgres can't put a
  real FK on a view

This is [OverlayFS](https://docs.kernel.org/filesystems/overlayfs.html)
applied to Postgres: a writable "upper" table merged with one read-only
"lower" table into one "merged" view. Each overlay model has exactly one
source table, though which physical table that is can be resolved per tenant
(see `get_source()` below) — it's a single, tenant-scoped source, not a
union of several.

## Usage

```python
from django.db import models

from django_overlay.fields import OverlayForeignKey
from django_overlay.models import OverlayMeta, OverlayModel
from django_overlay.sources import SourceTable


class Person(OverlayModel):
    first_name = models.CharField(max_length=100)
    age = models.IntegerField(null=True)

    class OverlayMeta(OverlayMeta.with_strategy(OverlayModel.Strategy.NEGATIVE_ID)):
        table_name = "person"  # optional, defaults to the lowercased class name

        @staticmethod
        def get_source():
            # Runs at migration-apply time. Put your own per-tenant lookup
            # here — django_overlay doesn't care where it resolves from.
            # Return None for a pure-organic model with no source table.
            return SourceTable(schema="external_source", table="people")


class Conversation(models.Model):
    # The only legal way to point a FK at an OverlayModel — a plain
    # ForeignKey here fails `manage.py check`.
    person = OverlayForeignKey(Person, on_delete=models.DO_NOTHING, related_name="conversations")
```

`class OverlayMeta(OverlayMeta.with_strategy(...))` looks odd but is normal Python scoping: the
base class expression evaluates against the *imported* `OverlayMeta` before this inner
class shadows that name. If you don't care which strategy you get (see below), just write
`class OverlayMeta(OverlayMeta):` — plain inheritance, no `with_strategy()` needed.

`Person` then behaves like an ordinary model:

```python
Person.objects.filter(age__gte=40)                   # plain indexed columns, no COALESCE
Person.objects.create(first_name="Jane")             # goes straight into the writable table
Person.objects.filter(id=source_only_id).update(age=41)  # copies the row over, then applies the edit
person.delete()
```

## How it works

`class Person(OverlayModel)` becomes two real Django models:

- a hidden `managed=True` table (`person`) — the only thing ever written to
- `Person` itself, `managed=False`, bound to a view (`person_view`) that's a
  `UNION ALL` of the base table and the model's one declared `SourceTable`
  (if any)

Three `INSTEAD OF` triggers on the view make it writable:

- **INSERT** — assigns an id, inserts into the base table
- **UPDATE** — updates the base table row if it exists there already,
  otherwise inserts one built from the new values (copy-up)
- **DELETE** — deletes the base table row (see `sql_templates/triggers/instead_of_delete.sql.j2`
  if you'd rather deletes revert to the source row instead)

`OverlayForeignKey` sets `db_constraint=False` (Postgres can't FK a view)
and instead ships a `CREATE CONSTRAINT TRIGGER`, deferred to commit like a
real FK, checking the referenced id exists in the base table or a source.

Every statement lives as a `.sql.j2` template under `django_overlay/sql_templates/`
(`view/`, `triggers/`, `ddl/`, `pk_defaults/`, `introspection/`) — `sql.py`
only computes context data (column lists, booleans, table names) and
renders; no SQL text is assembled with Python string formatting anywhere.

## Many-to-many relations

Django's plain `ManyToManyField` always builds its hidden through table with
a plain `ForeignKey` — there's no hook to make it use anything else. So a
plain `ManyToManyField` pointing at an overlay model quietly creates exactly
the kind of unconstrained FK this package exists to prevent (and
`django_overlay.checks` fails `manage.py check` if you do this — it looks
inside auto-created through tables too, not just your own models).

This isn't only caught by `manage.py check`, though — `manage.py check`
requires someone to remember to run it, and plenty of real deployments
(a gunicorn/uwsgi worker, a Celery worker) never call it at all.
`DjangoOverlayConfig.ready()` runs the same scan itself and raises
`ImproperlyConfigured` if it finds anything, so the process refuses to boot
at all — `django.setup()` fails the same way for `runserver`, `migrate`,
`manage.py shell`, a WSGI worker, everything.

`OverlayManyToManyField` is a `ManyToManyField` that requires an explicit
`through=` model — there's no auto-created-through-table option to begin
with, so there's nothing unsafe to accidentally reach for. Write the through
model the same way you would for any Django M2M that needs one, using
`OverlayForeignKey` for the side(s) that point at an overlay model
(`Organization` is an overlay model like `Person`; both are already defined
elsewhere in this sketch):

```python
from django_overlay.fields import OverlayForeignKey, OverlayManyToManyField


class Membership(models.Model):
    person = OverlayForeignKey(Person, on_delete=models.CASCADE)
    organization = OverlayForeignKey(Organization, on_delete=models.CASCADE)
    role = models.CharField(max_length=100)  # extra fields on the relationship go here


class Person(OverlayModel):
    organizations = OverlayManyToManyField(Organization, through=Membership)
```

Omitting `through=` is a `TypeError` at class-definition time, not a silent
unsafe default. Works with the ORM as usual —
`person.organizations.add(org, through_defaults={"role": "member"})`, `.all()`,
reverse accessors, etc.

## Ids: keeping the client side and the source side from colliding

An organic `Person.objects.create(...)` and an untouched source row must
never end up with the same id — otherwise the view's "not yet materialized"
check would treat them as the same row and one would silently hide the
other. `OverlayMeta.strategy` (`OverlayModel.Strategy` / `OverlayMeta.Strategy`)
picks how: `OverlayMeta` on its own defaults to `Strategy.UUID4`; use
`OverlayMeta.with_strategy(...)` for anything else.

That `Strategy.UUID4` default is itself overridable project-wide with
`settings.DJANGO_OVERLAY_DEFAULT_STRATEGY = Strategy.NEGATIVE_ID` (or
whichever), so a project that wants one strategy everywhere doesn't need
`.with_strategy(...)` on every single model — read once, at import time, so
it has to be set before any app's `models.py` is imported (i.e. in
`settings.py` itself, not somewhere conditional at runtime). A model can
still opt out with its own `.with_strategy(...)` regardless of this setting.

- **`Strategy.NEGATIVE_ID`** — integer pk (Django's default `AutoField`).
  Source rows are shown in the view with their id *negated*: the base
  table's own Postgres sequence only ever hands out positive ids to organic
  creates, so positive ids are always client rows and negative ids are
  always source-derived, with no coordination between the two tables
  needed. `Person.objects.get(id=-42)` reaches source row 42;
  `Person.objects.create(...)` always gets a positive id.
- **`Strategy.UUID4`** (the default) — random uuid pk. No negation needed;
  uuids don't collide by construction. Falls back to Postgres's built-in
  `gen_random_uuid()` (Postgres 13+) whenever an insert doesn't supply one.
- **`Strategy.UUID7`** — time-ordered uuid pk (doesn't fragment a btree
  index the way pure-random v4 ids do). Falls back to Postgres 18's native
  `uuidv7()`. **Only pick this if you're already on Postgres 18+**: PL/pgSQL
  has to resolve every function name in a trigger body to compile it at
  all, so if `uuidv7()` doesn't exist, *every* insert into that table fails
  — not just id-less ones that would actually hit the fallback.
- **`Strategy.UUID7_POLYFILL`** — same time-ordered uuid pk, but the
  fallback is a portable SQL expression (built from `gen_random_uuid()` +
  `clock_timestamp()`, no extension needed) instead of Postgres 18's native
  function. Use this unless you know you're on Postgres 18+.

For any UUID strategy, the field itself is auto-declared for you if you
don't declare your own `id` — you only need to add one yourself for
custom options (e.g. a different field name).

The chosen strategy's Postgres-side fallback (`gen_random_uuid()`,
`uuidv7()`, the polyfill, or `nextval(sequence)` for `NEGATIVE_ID`) only
ever runs when nothing supplies an id before the trigger sees it. A
field-level `default=uuid.uuid4` (what gets auto-declared for `UUID4`, and
the usual recommended setup — it's what makes
`Person.objects.create(...).id` available immediately in Python) means
Django assigns the id itself before the INSERT, so the SQL-side fallback
never fires for ordinary `.create()` calls; it's there for whatever
bypasses that — raw SQL, bulk paths, or a pk field declared with no
Python-side default at all. Override the fallback expression directly with
`OverlayMeta.pk_default_sql` (a raw SQL string) if you want a specific
generator regardless of strategy. Drop the field's Python-side default if
you want Postgres to be the sole id authority, matching how
`NEGATIVE_ID` already works — but then Django won't read the generated id
back onto the object it returns; you'd need `obj.refresh_from_db()` to see
it.

## Uniqueness across the whole view

A plain `models.UniqueConstraint` in `Meta.constraints` only ever guards the
base table against itself — it can't stop a base row from colliding with a
value that already exists in the source table, since Postgres can't put a
`UNIQUE` constraint across a view's `UNION ALL`. `OverlayUniqueConstraint`
(same `fields=[...]`/`name=` signature as `UniqueConstraint`, since it *is*
one) adds that missing half:

```python
from django_overlay.constraints import OverlayUniqueConstraint


class Person(OverlayModel):
    ssn = models.CharField(max_length=11)

    class Meta:
        constraints = [OverlayUniqueConstraint(fields=["ssn"], name="person_ssn_unique")]
```

Base-vs-base is enforced for free by Postgres's own real `UNIQUE` constraint
(forwarded to the base model like any other `Meta.constraints` entry) —
the *new* part is a deferred constraint trigger that also rejects a base row
whose value already exists, untouched, in the source table. It's a snapshot
check at write time, not a standing guarantee: a source row that starts
colliding with an already-materialized base row *after* the fact (vendor
data drifting post-hoc) isn't retroactively caught — same class of
limitation as the FK-safety trigger only checking at write time.

**Index the source table's constrained column(s) yourself.** The trigger's
existence check queries a table django-overlay doesn't own the DDL for, so
it can't create that index for you the way it creates one automatically for
the base table (a `UniqueConstraint` is backed by a real unique index;
`OverlayForeignKey` defaults to `db_index=True` same as a plain
`ForeignKey`) — both sides *we* control are already indexed without you
doing anything. Benchmarked on a 500k-row source table on the same
machine: with an index on the constrained column, an insert into the
overlay model runs about **1.2x** the time of the same insert into a plain
table with just a native `UniqueConstraint` (~0.07ms vs ~0.06ms). Without
that index, the same insert took **~150x** longer (~10ms) — the trigger's
`EXISTS` degrades to a sequential scan of the whole source table on every
write.

## Migrations

`makemigrations` is overridden: whenever a migration changes an
OverlayModel's fields, or adds/renames/removes an `OverlayForeignKey`
(including one inside an `OverlayManyToManyField`'s through model) or an
`OverlayUniqueConstraint`, it appends the operation that regenerates the
view/triggers or adds/drops the constraint trigger. The view can't drift
out of sync with the model — including cleanup: removing an
`OverlayForeignKey` field or an `OverlayUniqueConstraint` drops its trigger
too, not just leaves it silently checking against a column/constraint that
no longer exists.

One thing none of this can catch: if which source a tenant is configured to
use changes (e.g. moved from one vendor's table to another's) without a
field change, that's a data change, not a schema change. Call
`django_overlay.sync.resync_view(model)` yourself when that happens — e.g.
from your own command or a signal handler — or run the bundled
`manage.py resync_overlay_views app_label.ModelName [...]` command.

## Left to you

- **Where the source comes from** — `OverlayMeta.get_source()` is a plain
  function; hardcode it, read from your own config model, whatever. It's
  expected to resolve to the same table every time for a given tenant once
  set — switching it is a deliberate, explicit resync (see above), not
  something the package detects on its own.
- **Filtering a source's rows** (e.g. giving a tenant a subscription-tier
  subset of one table rather than all of it) — `SourceTable.extra_where` is
  the hook; it's raw SQL, spliced directly into the view's `CREATE VIEW`
  text (a view's SQL is static, Postgres has no bind parameters for it).
  A hardcoded literal you wrote yourself is fine as-is. If the filter value
  instead comes from *data* (a subscription/entitlement lookup), don't
  f-string it in directly — build the literal with a real SQL-quoting
  helper (e.g. `psycopg.sql.Literal(value).as_string(connection)`) before
  putting it in `extra_where`, the same way you'd avoid string-formatting
  user input into any other raw SQL string.
- **Delete semantics** — hard delete is the default.
- **Multi-tenancy** — uses `connection.schema_name` if `django_tenants` is
  installed, otherwise falls back to Postgres's own `current_schema()`.

`Meta.permissions`/`Meta.default_permissions` aren't supported at all —
declaring either raises `OverlayConfigurationError` at class-definition
time. Neither side is a sound place to forward them: the view model is
`managed=False`, so Django's `create_permissions()` silently skips it and
never creates the `Permission` rows at all; the hidden base model would
create them, but under a codename tied to a model application code should
never touch directly. The base model's own default permissions are always
suppressed (`default_permissions = ()`), so it doesn't clutter permission
lists with entries for a table nobody's supposed to reference.

## Running the tests

Needs a real Postgres instance — this package has no SQLite path.

```bash
uv sync
POSTGRES_USER=postgres uv run pytest
```

The main suite covers `NEGATIVE_ID`/`UUID4`/`UUID7_POLYFILL` (the
practically-testable strategies — native `UUID7` needs Postgres 18+ and is
only covered by pure-Python/SQL-string unit tests) across a
People/Address/Phone model set: both M2M styles, a one-to-one and a plain
FK bonus relation, and the checks that reject unsafe FK/M2M usage.

`tests/test_tenants/` proves the same mechanism works under `django_tenants`
schema-per-tenant multi-tenancy (source tables shared in `public`, overlay
models per-tenant) — it needs its own settings module and database, so it's
a separate invocation, not part of the command above:

```bash
DJANGO_SETTINGS_MODULE=tests.django_tenants_settings POSTGRES_USER=postgres \
  uv run pytest tests/test_tenants -o addopts="" --create-db
```
