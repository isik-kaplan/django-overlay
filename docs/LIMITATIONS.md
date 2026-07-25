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
- **Delete semantics** — see [DELETION.md](DELETION.md).
- **Multi-tenancy** — uses `connection.schema_name` if `django_tenants` is
  installed, else Postgres's own `current_schema()`.

`Meta.permissions`/`Meta.default_permissions` aren't supported — declaring
either raises `OverlayConfigurationError`. Neither model is a sound home for
them: the view model is `managed=False` so `create_permissions()` silently
skips it; the hidden base model would create them under a codename tied to
a table application code should never reference directly. The base model's
own default permissions are always suppressed (`default_permissions = ()`).
