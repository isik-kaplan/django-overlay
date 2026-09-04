# Migrations

## The command has to be ours

`django_overlay` ships its own `makemigrations`, and Django resolves a command
name to whichever app declares it *last* in `INSTALLED_APPS`. If another app
overrides `makemigrations` too (`django_tenants` does, as `makemigrations` and
`migrate_schemas`), the last one wins and the other is simply not used — with
no warning. If that were `django_overlay`'s, the view and trigger operations
would silently stop being generated and your migrations would look fine while
doing half the work.

Put `django_overlay` **after** any other app that overrides the command, and
check with:

```bash
python manage.py makemigrations --help   # the description names django_overlay
```

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
