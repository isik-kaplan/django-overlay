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

`OverlayManyToManyField` requires an explicit `through=` model — there's no
auto-created-through-table option to accidentally reach for. Write the
through model like any Django M2M that needs one, using `OverlayForeignKey`
for the side(s) pointing at an overlay model:

```python
from django_overlay.fields import OverlayForeignKey, OverlayManyToManyField


class Membership(models.Model):
    person = OverlayForeignKey(Person, on_delete=models.CASCADE)
    organization = OverlayForeignKey(Organization, on_delete=models.CASCADE)
    role = models.CharField(max_length=100)  # extra fields on the relationship go here


class Person(OverlayModel):
    organizations = OverlayManyToManyField(Organization, through=Membership)
```

Omitting `through=` is a `TypeError` at class-definition time. Works with the
ORM as usual — `.add(org, through_defaults={"role": "member"})`, `.all()`,
reverse accessors, etc.
