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
    # The only legal way to point a FK at an OverlayModel. on_delete works as
    # it always does; DO_NOTHING will raise IntegrityError if the target is
    # still referenced, exactly as it would against a real foreign key.
    person = OverlayForeignKey(Person, on_delete=models.CASCADE, related_name="conversations")
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

Raw SQL that updates the writable table only sees rows that table already
holds. When it must also reach source rows the tenant hasn't touched yet, copy
them down first:

```python
Person.objects.filter(id__in=ids).materialize()      # source rows copied over; existing ones left alone
```

## Shared fields on an abstract parent

Fields an overlay model inherits from an abstract parent are handled exactly
like fields declared on it, whether the parent derives from `OverlayModel` or
is a plain `models.Model` mixed in beside it:

```python
class BaseModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False, db_comment="Row id.")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="When the row was made.")

    class Meta:
        abstract = True


class Person(BaseModel, OverlayModel):
    first_name = models.CharField(max_length=100)

    class OverlayMeta(OverlayMeta.with_strategy(OverlayModel.Strategy.UUID7)):
        @staticmethod
        def get_source():
            return SourceTable(schema="external_source", table="people")
```

- An inherited primary key is kept, comment and all; the strategy only adds an
  `id` when nothing declares a primary key. It has to suit the strategy; see
  [IDS.md](../concepts/IDS.md#the-primary-key-has-to-suit-the-strategy).
- An inherited `ForeignKey` or `OneToOneField` claims no reverse accessor from
  the hidden base model, and a one-to-one is stored there as a plain foreign
  key, its uniqueness coming from the `OverlayUniqueConstraint` the model
  declares, as for one declared on the model.
- An inherited many-to-many is on the view model only.
- An inherited field named `_overlay_deleted` is refused, as a declared one is.

Before 0.3.0 only the class body got this treatment: an inherited primary key
was replaced by the strategy's, losing its comment, and an inherited relation
was copied onto the base model as it was. A project upgrading with relations
or a primary key on an abstract parent gets a migration from `makemigrations`
that brings the base table in line.
