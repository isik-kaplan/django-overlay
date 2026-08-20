# Deleting a row

`.delete()` behaves like a normal model's delete: the row stays gone. That is
`OverlayMeta.soft_delete`, which is **on by default** — it adds a hidden
`_overlay_deleted` flag to the base table, and the view excludes flagged rows
everywhere.

It has to be a flag rather than a real delete because the row may not be yours:
deleting your local copy of a source-backed row would just un-mask the vendor's
original, and it would reappear.

Set `soft_delete = False` (or `settings.DJANGO_OVERLAY_DEFAULT_SOFT_DELETE`)
for the other behaviour, described next. It is worth considering for a purely
organic model — with no source row to keep masked, a tombstone buys nothing and
holds an index entry forever.

## With `soft_delete = False`

`.delete()` only removes the base table's copy — for a purely organic row (no source
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

A soft-deleted row stays in the base table as a hidden tombstone, so every
uniqueness rule on a `soft_delete` model is emitted as a *partial* index
(`WHERE NOT _overlay_deleted`) and the source-side trigger skips source rows a
tombstone is masking. The upshot is that a soft delete frees the row's unique
values for reuse, exactly as deleting from a real table would. That's also why
uniqueness on these models has to be declared through `Meta.constraints` —
see [UNIQUENESS.md](UNIQUENESS.md).

The one thing a soft delete doesn't free is the row's **primary key**: a
partial primary key isn't a thing in Postgres. It only matters if you assign
pks explicitly — organic rows take theirs from a sequence and source-backed
rows keep the identity they already had, so neither reuses one.
