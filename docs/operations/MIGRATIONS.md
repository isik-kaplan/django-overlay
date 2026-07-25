# Migrations

`makemigrations` is overridden: whenever a migration changes an
OverlayModel's fields, or adds/renames/removes an `OverlayForeignKey`
(including inside an `OverlayManyToManyField`'s through model) or an
`OverlayUniqueConstraint`, it appends the operation that regenerates the
view/triggers or adds/drops the constraint trigger — including cleanup:
removing a field or constraint drops its trigger too, not just leaves it
checking against something that no longer exists.

One thing this can't catch: a tenant's configured source changing (e.g.
moved vendors) without a field change — that's a data change, not a schema
change. Call `django_overlay.sync.resync_view(model)` yourself when that
happens, or run `manage.py resync_overlay_views app_label.ModelName [...]`.
