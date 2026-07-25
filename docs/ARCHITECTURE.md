# How it works

`class Person(OverlayModel)` becomes two real Django models:

- a hidden `managed=True` table (`person`) — the only thing ever written to
- `Person` itself, `managed=False`, bound to a view (`person_view`) that's a
  `UNION ALL` of the base table and the model's one declared `SourceTable`
  (if any)

Three `INSTEAD OF` triggers on the view make it writable:

- **INSERT** — assigns an id, inserts into the base table
- **UPDATE** — updates the base table row if it exists there already,
  otherwise inserts one built from the new values (copy-up)
- **DELETE** — see [DELETION.md](DELETION.md)

`OverlayForeignKey` sets `db_constraint=False` (Postgres can't FK a view)
and instead ships a `CREATE CONSTRAINT TRIGGER`, deferred to commit like a
real FK, checking the referenced id exists in the base table or a source.

Every statement lives as a `.sql.j2` template under `django_overlay/sql_templates/`
(`view/`, `triggers/`, `ddl/`, `pk_defaults/`, `introspection/`) — `sql.py`
only computes context data (column lists, booleans, table names) and
renders; no SQL text is assembled with Python string formatting anywhere.
