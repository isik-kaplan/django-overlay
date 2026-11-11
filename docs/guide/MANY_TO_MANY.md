# Many-to-many relations

Django's plain `ManyToManyField` always builds its hidden through table with
a plain `ForeignKey` — there's no hook to make it use anything else. A plain
`ManyToManyField` pointing at an overlay model would quietly create exactly
the unconstrained FK this package exists to prevent, so
`django_overlay.checks` fails `manage.py check` for it (including inside
auto-created through tables).

That alone isn't enough — `manage.py check` requires someone to remember to
run it, and plenty of real deployments (gunicorn, Celery) never do.
`DjangoOverlayConfig.ready()` runs the same scan at process boot and raises
`ImproperlyConfigured` if it finds anything, so the process refuses to start
at all — for `runserver`, `migrate`, `shell`, a WSGI worker, everything.

## `OverlayManyToManyField` builds its own through model

Without `through=`, `OverlayManyToManyField` builds the through model itself,
like Django's `ManyToManyField`, with one difference: each end is decided by
the class it points at, never by data or settings.

| Declared on → pointing at | link to the declaring model | link to the target |
|---|---|---|
| overlay → plain | `OverlayForeignKey` | `ForeignKey` |
| overlay → overlay | `OverlayForeignKey` | `OverlayForeignKey` |
| plain → overlay | `ForeignKey` | `OverlayForeignKey` |
| overlay → itself (`"self"`) | `OverlayForeignKey` (`from_…`) | `OverlayForeignKey` (`to_…`) |

```python
from django_overlay.fields import OverlayManyToManyField


class Person(OverlayModel):
    tags = OverlayManyToManyField(Tag, blank=True)            # overlay → plain
    friends = OverlayManyToManyField("self", blank=True)      # overlay → itself


class Team(models.Model):
    members = OverlayManyToManyField(Person, related_name="teams")  # plain → overlay
```

- **Names are Django's.** The through model is `<Model>_<field>`, its columns
  `<model>_id` and `<target>_id` (`from_<model>_id` / `to_<model>_id` for
  `"self"`), and its table `<declaring table>_<field>`, or the field's own
  `db_table=`. For an overlay model the declaring table is its *base* table,
  not its view: `Person.tags` above is `person_tags`, the same table a plain
  `ManyToManyField` on the model had before it became an overlay model.
- **It stays auto-created.** `through._meta.auto_created` is set, as for
  Django's own, so the field behaves like a plain many-to-many everywhere that
  reads it: Django's admin and model forms show and save it, and DRF's
  `ModelSerializer` keeps it writable. (Both make a field with an explicit
  `through=` read-only.)
- **Links to source rows work as they are.** Linking a row that only exists in
  the source table writes a row in the through table and leaves the source row
  where it is; the `OverlayForeignKey` end checks it with its trigger.
- **No permissions**, as for Django's own through models.

### In migrations

Django creates an auto-created through table along with the model declaring the
field, and skips an unmanaged model, which an overlay model's view is. So the
through models this field builds go into migration state as models of their
own: `makemigrations` writes a `CreateModel` for each, with
`AddOverlayConstraint` for every `OverlayForeignKey` end, and a `DeleteModel`
when the field is removed. From there they replay, squash and roll back like
any other model. Your migrations therefore name them (`Person_tags`), and a
plain model's field names its through model in the migration
(`through='app.Team_members'`).

### With an explicit `through=`

Works as before, for a relation that carries columns of its own or wants its
links chosen by hand. Use `OverlayForeignKey` for the side(s) pointing at an
overlay model:

```python
from django_overlay.fields import OverlayForeignKey, OverlayManyToManyField


class Membership(models.Model):
    person = OverlayForeignKey(Person, on_delete=models.CASCADE)
    organization = OverlayForeignKey(Organization, on_delete=models.CASCADE)
    role = models.CharField(max_length=100)  # extra fields on the relationship go here


class Person(OverlayModel):
    organizations = OverlayManyToManyField(Organization, through=Membership)
```

Works with the ORM as usual — `.add(org, through_defaults={"role": "member"})`,
`.all()`, reverse accessors, etc.

### Switching an explicit `through=` to a built one

To Django this is one model deleted and another created on the same table,
and the migration it would write drops the table and creates it again, empty.
`makemigrations` refuses to write it and says so. Write that migration by
hand instead: wrap the `DeleteModel` of your through model and the
`CreateModel` of the built one in
`migrations.SeparateDatabaseAndState(state_operations=[...])`, and add as
`database_operations` whatever the table needs to match the built model, such
as the unique constraint on the pair, which a hand-written through model may
not have had.
