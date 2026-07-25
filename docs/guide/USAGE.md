# Usage

```python
from django.db import models

from django_overlay.fields import OverlayForeignKey
from django_overlay.models import OverlayMeta, OverlayModel
from django_overlay.sources import SourceTable


class Person(OverlayModel):
    first_name = models.CharField(max_length=100)
    age = models.IntegerField(null=True)

    class OverlayMeta(OverlayMeta.with_strategy(OverlayModel.Strategy.NEGATIVE_ID)):
        table_name = "person"  # optional, defaults to the lowercased class name

        @staticmethod
        def get_source():
            # Runs at migration-apply time. Put your own per-tenant lookup
            # here. Return None for a pure-organic model with no source table.
            return SourceTable(schema="external_source", table="people")


class Conversation(models.Model):
    # The only legal way to point a FK at an OverlayModel.
    person = OverlayForeignKey(Person, on_delete=models.DO_NOTHING, related_name="conversations")
```

`class OverlayMeta(OverlayMeta.with_strategy(...))` looks odd but is normal
Python scoping — the base class expression resolves against the *imported*
`OverlayMeta` before this inner class shadows the name. If you don't care
which id strategy you get (see [IDS.md](../concepts/IDS.md)), plain
`class OverlayMeta(OverlayMeta):` works too.

`Person` then behaves like an ordinary model:

```python
Person.objects.filter(age__gte=40)                   # plain indexed columns, no COALESCE
Person.objects.create(first_name="Jane")             # goes straight into the writable table
Person.objects.filter(id=source_only_id).update(age=41)  # copies the row over, then applies the edit
person.delete()                                      # see DELETION.md
```
