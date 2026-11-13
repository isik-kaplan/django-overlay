# Updates that change nothing

The first edit of a source row copies it into your base table. That is what
an edit is. But an UPDATE writes every column it is given whether or not the
value changed, so a `save()` after only reading a row is an UPDATE too, and on a
row that exists only in the source it copies the row in for good. So does a
`PATCH` with nothing new in it, a signal handler that saves, or `update()` over a
queryset, which does it to every row it matches in one statement. A model with
an `auto_now` field makes it unconditional: that column always differs, so no
save is ever a no-op.

Whether that matters, which columns don't count, and what to do instead are
the project's to decide. django-overlay decides none of them: by default an
update that changes nothing behaves like any other update.

## The settings

Three settings give the project's defaults, and three `OverlayMeta` options
override them per model. A model's own value wins, including an empty one.

| `settings`                              | `OverlayMeta`             | Values                                         | Default         |
|-----------------------------------------|---------------------------|------------------------------------------------|-----------------|
| `DJANGO_OVERLAY_REDUNDANT_UPDATES`      | `redundant_updates`       | `"materialize"`, `"skip"`, `"warn"`, `"raise"` | `"materialize"` |
| `DJANGO_OVERLAY_REDUNDANT_UPDATE_SCOPE` | `redundant_update_scope`  | `"source_only"`, `"all"`                       | `"source_only"` |
| `DJANGO_OVERLAY_REDUNDANT_UPDATE_IGNORE`| `redundant_update_ignore` | field names                                    | none            |

- **Mode.** `"materialize"` writes it like any update. `"skip"` writes nothing
  and reports the row as updated. `"warn"` skips and says so: a Postgres
  `WARNING` from the trigger, a Python `UserWarning` from the expression path
  below. `"raise"` refuses, with `ProgrammingError` (`OverlayRedundantUpdate`
  from the expression path; both messages start with "overlay redundant
  update").
- **Scope.** `"source_only"` applies the mode only where the update would copy a
  source row into the base table. `"all"` also applies it to rows already there,
  where a redundant update would only have been a wasted write. The difference
  shows in a column like `updated_at`: under `"source_only"` a redundant save
  still moves it on your own rows and not on vendor rows.
- **Ignored fields.** Left out of the comparison: an update that changes only
  these changes nothing. A project-wide name a model doesn't have is skipped;
  one named on a model that doesn't have it fails `manage.py check`
  (`django_overlay.E008`), because a misspelt name would quietly keep every
  update counting as a change.

```python
# settings.py: every overlay model
DJANGO_OVERLAY_REDUNDANT_UPDATES = "skip"
DJANGO_OVERLAY_REDUNDANT_UPDATE_IGNORE = ["updated_at"]


class Person(OverlayModel):
    ...

    class OverlayMeta(OverlayMeta):
        redundant_update_scope = "all"  # this model only
```

"Skip" never makes an update report zero rows. Django answers an UPDATE that
touched nothing by trying an INSERT, or, with `update_fields`, by raising
`DatabaseError`, so the trigger reports the row it skipped as updated.

## Where it applies

- **`save()`, `update()` with values, `bulk_update()`**: through the view's
  `INSTEAD OF UPDATE` trigger, row by row, comparing each new value with the
  row as the view showed it.
- **An update whose value reads the row** (`update(n=F("n") + 1)`, or
  `obj.n = F("n") + 1; obj.save()`): routed around the view for atomicity, it
  copies every matched row into the base table before updating them there.
  Under a mode other than `"materialize"` it first leaves out the rows the
  update would not change, compared the same way, so they are not copied
  either. That comparison is made when the statement reads the rows, before
  they are locked.

The policy is part of the trigger body, so changing it, in settings or in
`OverlayMeta`, takes `manage.py resync_overlay_views` to apply to an existing
database. It generates no migration.
