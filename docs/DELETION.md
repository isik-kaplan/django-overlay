# Deleting a row

By default, `.delete()` doesn't behave like a normal model's delete. It only
removes the base table's copy — for a purely organic row (no source
counterpart), that's final; for a source-backed row, removing the base copy
just un-masks the original in the view's `UNION ALL`. So "delete" reverts to
the source's pristine data if the row was ever materialized (discarding any
edits), or is a no-op if it was never touched. `on_delete=CASCADE/PROTECT/SET_NULL`
on an `OverlayForeignKey` pointing at it still work exactly like a normal
model — Django's own delete collector runs against the view model (what
every `OverlayForeignKey` actually points at) before any of this SQL runs.

For a real, permanent delete — one where the row stays gone even with a
source counterpart — opt in with `OverlayMeta.soft_delete = True` (or
`settings.DJANGO_OVERLAY_DEFAULT_SOFT_DELETE = True` project-wide, same
pattern as [IDS.md](IDS.md)'s strategy setting). This adds a hidden
`_overlay_deleted` flag on the base table only (never exposed on the view) —
`.delete()` now flags instead of touching data, the view excludes flagged
rows everywhere, and a soft-deleted row can no longer be a target for a
brand-new `OverlayForeignKey`.

`reset_to_source()` is the explicit escape hatch either way: discards a
row's local materialization/soft-deletion, falling back to whatever the
source shows for that id (nothing, if there's no source row). Deliberately
**not** a delete — the identity is often not actually going away, just
resolving via source instead — so it skips Django's `on_delete` collector
entirely. Safe for a source-backed row; for a purely organic row with
dependents, this can leave a reference that only the deferred FK-constraint
trigger catches, and only on its *next* write. Use `.delete()` instead if
you need `on_delete` safety.

Known limitation either way: an `OverlayUniqueConstraint`'s value stays
reserved forever after a soft delete — it's a plain (non-partial) index, so
the still-present flagged row keeps blocking reuse of that value.
