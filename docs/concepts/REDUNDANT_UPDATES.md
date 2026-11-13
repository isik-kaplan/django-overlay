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
  below. `"raise"` refuses, with `OverlayRedundantUpdate`.
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
  either.

A refused update raises `OverlayRedundantUpdate`, a `ProgrammingError`, on
every ORM path. Raw SQL through the view gets the trigger's error itself, with
SQLSTATE `OV001`: a code of django-overlay's own, so it can't be mistaken for
another trigger's `RAISE EXCEPTION` (`P0001`). Both messages start with
`overlay redundant update`.

## Concurrent writes

The trigger only ever writes the columns an update changed, compared with the
row as the statement read it, whether or not the guard is on: an update that
sets a column to the value it already had writes nothing to it. That is why the
guard is safe through the trigger and needs care on the expression path.

The comparison is made against the row as the statement read it, before it is
locked.

- **Through the trigger** this changes nothing a concurrent writer could
  notice. The trigger only ever writes the columns that differ from the row it
  read, with or without the guard, so an update it skips would have written
  nothing to a row another transaction changed in between either.
- **On the expression path** it can. Without the guard, the update re-reads
  each row once it holds the lock and writes the expression's value. With it,
  a row left out because the value matched what was read is not written, even
  if another transaction changed it before this one would have taken the lock.
  The two can end differently under `READ COMMITTED`, Django's default. Only
  for a value that can equal the current one: `F("n") + 1` never does. Run the
  transaction under `SERIALIZABLE`, which detects the conflict and fails one
  side, where that matters.

  The rows left out are read once, so the count a warning or a refusal reports
  is exactly the set left out.

## Applying a change

The policy is part of the `INSTEAD OF UPDATE` trigger. Changing it, in
settings or in `OverlayMeta`, generates no migration and takes effect in an
existing database only when `manage.py resync_overlay_views` rebuilds the
triggers. **Nothing warns you if that hasn't been run:** the settings say one
thing and the database keeps doing another. The expression path reads the
policy from settings at run time, so until the resync the two paths disagree.
