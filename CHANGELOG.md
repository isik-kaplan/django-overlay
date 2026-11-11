# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking

- **A primary key the id strategy can't fill is refused when the model is declared.** A UUID strategy needs a
  `UUIDField`, the integer strategies an integer field; new rows get their id from the strategy's column default, so a
  mismatch used to surface only as a failed insert. A primary key that is a link is exempt, and so is a model that sets
  `OverlayMeta.pk_default_sql`.

### Fixed

- **Fields inherited from an abstract parent are handled like fields declared on the model.** An inherited primary key
  is no longer replaced by the strategy's, so it keeps its `help_text` and `db_comment`. An inherited relation no
  longer claims a reverse accessor from the hidden base model, an inherited one-to-one no longer leaves a table-level
  unique constraint on it, and an inherited many-to-many is no longer added to it. Run `makemigrations` if a model
  inherits any of these.
- **The columns django-overlay adds have a `help_text` and a `db_comment`:** the primary key a UUID strategy adds, and
  `_overlay_deleted`. Run `makemigrations` to record the comments.
- **A column's comment can be changed on an overlay model's base table.** Django changes a comment with an
  `ALTER COLUMN ... TYPE`, which Postgres refuses on a column the view selects; `makemigrations` now writes an
  `AlterOverlayField` there, which runs only `COMMENT ON COLUMN` when the comment is all that changed. It also puts
  back a unique index Django drops while altering a field: turning a `ForeignKey` into a `OneToOneField`, which is what
  rolling back the inherited one-to-one upgrade does, used to drop the base table's half of an
  `OverlayUniqueConstraint` with it.

## [0.2.0] - 2026-11-04

### Breaking

- **Django 6.0 and Python 3.12 are the floor.** 0.1.0 declared Django 4.2 and Python 3.11, but on anything before 6.0
  the first update touching a column it reads raised `TypeError`. Upgrade Django before upgrading this.
- **Links into an overlay model are enforced.** `OverlayForeignKey` writes deferred constraint triggers. An insert that
  points at a row the view doesn't show, or a delete or soft delete of a row something still points at, raises
  `IntegrityError` at commit, raw SQL included. Run `makemigrations` to add the triggers, and fix any dangling links
  first.
- **Soft delete is the default, and is decided per row.** `DJANGO_OVERLAY_DEFAULT_SOFT_DELETE` now defaults to `True`.
  A deleted row that hides a source row keeps a tombstone; one with nothing behind it is deleted outright. Set the
  setting, or `soft_delete = False` in a model's `OverlayMeta`, to keep hard deletes.
- **An overlay model must have a source table.** One without is refused when it is declared; make it a plain
  `models.Model`, which `OverlayForeignKey` can still point at.
- **`select_related()` across overlay models runs as `prefetch_related()`,** two queries over one view each instead of
  one over a join. Combining it with `union()`, `intersection()`, `difference()` or an unchunked `iterator()` is
  refused rather than left slow.

### Added

- **Two integer strategies that use ids as stored.** `Strategy.NEGATIVE_SOURCE_POSITIVE_BASE_ID` is for a source that
  already keeps negative ids, beside a base table counting up; `NEGATIVE_BASE_POSITIVE_SOURCE_ID` is its mirror, with
  the base sequence counting down from -1. Nothing is negated, so the source's own foreign keys keep naming the rows
  the view shows. See [docs/concepts/IDS.md](docs/concepts/IDS.md).
- **`Strategy.UUID7`**, and `UUID7_POLYFILL` for Postgres before 18.
- **Asking where a row comes from.** `with_origin()`, `base_only()`, `source_only()`, `overridden()` and `organic()`
  on the queryset and manager. `base_only()` and `source_only()` drop the other half of the view from the plan, and
  are the cheapest queries there are.
- **The vendor's own row.** `Model.source_table()` is a read-only model over the source, with its `extra_where`
  applied unless you ask for `.unfiltered()`, and `instance.source_row()` is the row behind an override, read without
  touching it.
- **`materialize()`** on the queryset copies the matched source rows into the base table, for code that writes the base
  table with raw SQL.
- **`OverlayMeta.overridable = False`** for a model whose source rows are never edited. The view skips the anti-join,
  and the triggers refuse an edit rather than trust it.
- **Partitioned sources.** `SourceTable(partition_key=...)` names the column a source is partitioned on, and
  `OverlayForeignKey(partition_column=...)` the referencing column carrying it, so the triggers' probes prune to one
  partition. System checks refuse a declaration that names a missing column.
- **`manage.py swap_source`**, for moving a model to a new source table: with `--candidate-*` it checks a table against
  the live source and changes nothing, without it it cuts over. A refusal raises `OverlaySwapRefused` with its report.
  See [docs/operations/SOURCE_SWAPS.md](docs/operations/SOURCE_SWAPS.md).
- **`manage.py show_source_indexes`**, and a system check that each source is indexed the way the view needs. The
  report counts a source's partitions and the indexes attached to none of them.
- **`overlay_selective()` and `pk__overlay_selective_in`**, for telling the planner a scope is small when it can't see
  so. See [docs/reference/QUERY_REWRITING.md](docs/reference/QUERY_REWRITING.md).
- **Settings to turn each rewrite off:** `DJANGO_OVERLAY_REWRITE_TRAVERSALS`, `DJANGO_OVERLAY_REDIRECT_SELECT_RELATED`,
  `DJANGO_OVERLAY_ARRAY_SUBQUERY_IN`, `DJANGO_OVERLAY_M2M_FENCE`, `DJANGO_OVERLAY_FORCE_HASH_JOINS`,
  `DJANGO_OVERLAY_LATE_ROW_LOOKUP` and `DJANGO_OVERLAY_ARRAY_LITERAL_IN`.
- **Overlay models keyed by a one-to-one link**, such as a geometry table keyed on its parent's id.

### Changed

- **Queries across overlay views are rewritten to stay fast.** `filter(fk__field=...)` becomes a semi-join over one
  view at a time; an m2m traversal gets an extra `= ANY (ARRAY(...))` bound; a statement reading several views runs
  with nested loops off; `count()` counts each half on its own; an ordered page resolves its keys before its rows; and
  a literal key list goes as one array parameter. Each is decided by the query's shape alone, and the measurements are
  in [docs/operations/PERFORMANCE.md](docs/operations/PERFORMANCE.md).
- **The first edit of a source row is two statements:** an insert of the row as the view showed it, then the edit. A
  row trigger or audit table on the base table can now tell which columns a tenant actually changed.
- **`resync_overlay_views` rebuilds everything that names the source,** the uniqueness and foreign-key triggers
  included, in one transaction. It keeps the view's column order, and when the new source changes a column's type it
  drops the view, without `CASCADE`, and builds it again.
- **Links from unmanaged models get no triggers,** since their tables aren't this project's to change.
- **`get_internal_type()`** on `OverlayForeignKey`, `OverlayOneToOneField` and `OverlayManyToManyField` reports
  `ForeignKey`, `OneToOneField` and `ManyToManyField`, so code checking a field's type treats them as the relations
  they are.

### Fixed

- **`update(age=F("age") + 1)` and `save()` no longer lose concurrent writes** through the view; the rows are copied
  into the base table and updated there, under its row locks.
- **A link to a source row a tombstone hides is refused,** like a link to any row the view doesn't show.
- **The triggers apply the source's `extra_where`**, so a row outside it can't be linked or counted as a duplicate.
- **The delete-side link check treats the partition key as a hint**, so a row whose key is null can still be changed
  while something points at it.
- **A target whose primary key has its own `db_column`** no longer breaks every write to the tables pointing at it.
- **Importing `django_overlay.fields` before `django_overlay.models`** no longer fails with a circular import.
- **A one-to-one primary key** no longer trips Django's `fields.W342` check.

## [0.1.0] - 2026-07-26

First release: writable Django models over read-only source tables you don't own, as one view with INSTEAD OF
triggers, `OverlayForeignKey`, `OverlayUniqueConstraint`, `reset_to_source()`, soft delete, and a `makemigrations`
that writes the views and triggers.
