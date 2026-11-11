# Migrations

## The command has to be ours

`django_overlay` ships its own `makemigrations`, and Django gives a command
name to the *first* app in `INSTALLED_APPS` that ships it. If another app
overrides `makemigrations` too and is listed first, its command is used and
`django_overlay`'s simply isn't, with no warning from Django: the view and
trigger operations would stop being generated, and your migrations would look
fine while doing half the work. (`django_tenants` ships `migrate`, not
`makemigrations`, so it doesn't compete for this one.)

Put `django_overlay` **before** any other app that overrides the command. The
system check `django_overlay.E006` fails `manage.py check` when another app's
`makemigrations` wins; a command of your own that subclasses
`django_overlay.management.commands.makemigrations.Command` passes it.

`makemigrations` is overridden: whenever a migration changes an
OverlayModel's fields, or adds/renames/removes an `OverlayForeignKey`
(including inside an `OverlayManyToManyField`'s through model) or an
`OverlayUniqueConstraint`, it appends the operation that regenerates the
view/triggers or adds/drops the constraint trigger — including cleanup:
removing a field or constraint drops its trigger too, not just leaves it
checking against something that no longer exists.

Deleting an OverlayModel entirely gets the same treatment: every
`DeleteModel` gets a view drop inserted immediately before it. Postgres
refuses to drop a table its view still depends on, so dropping the base
table without dropping the view first would fail with a dependency error.
This runs unconditionally for any `DeleteModel` (`DROP VIEW IF EXISTS` is a
no-op for a plain model that never had one), not just overlay ones.

One thing this can't catch: a tenant's configured source changing (e.g.
moved vendors) without a field change — that's a data change, not a schema
change. Call `django_overlay.sync.resync_view(model)` yourself when that
happens, or run `manage.py resync_overlay_views app_label.ModelName [...]`.
Both rebuild the view, its `INSTEAD OF` triggers *and* every constraint trigger
whose body names that source, in one transaction.

If the new source is a rebuilt copy of the old one rather than a different
vendor entirely — a blue-green load — `resync_overlay_views` is not enough on
its own, because nothing in it asks whether the new table still means the same
thing by an id. See [SOURCE_SWAPS.md](SOURCE_SWAPS.md).

## Changing a column's comment

Django changes a `db_comment` with `ALTER COLUMN ... TYPE` to the same type,
then `COMMENT ON COLUMN`, and Postgres refuses the first on any column a view
selects: every column of an overlay model's base table. So `makemigrations`
writes each `AlterField` on a base table as `AlterOverlayField`, which runs only
the `COMMENT ON COLUMN` when the comment is all that changed, and is a plain
`AlterField` otherwise. It decides when the migration runs, from the two
historical fields, so nothing extra is recorded in the migration file.

`AlterOverlayField` also puts back a unique index Django drops while altering a
field. When a field gains uniqueness, as a `ForeignKey` turned back into a
`OneToOneField` does, Django drops every plain btree index on its column, the
partial unique index behind an `OverlayUniqueConstraint` included.

A change of a column's *type* on a column the view selects is still refused by
Postgres. There is no migration path for that yet: drop the view, alter the
column and resync the view by hand, in one transaction.
