# Uniqueness across the whole view

A plain `models.UniqueConstraint` only guards the base table against itself
— it can't stop a base row from colliding with a value that already exists
in the source table (Postgres can't put a `UNIQUE` constraint across a
view's `UNION ALL`). `OverlayUniqueConstraint` (same signature as
`UniqueConstraint`, since it *is* one) adds that missing half:

```python
from django_overlay.constraints import OverlayUniqueConstraint


class Person(OverlayModel):
    ssn = models.CharField(max_length=11)

    class Meta:
        constraints = [OverlayUniqueConstraint(fields=["ssn"], name="person_ssn_unique")]
```

Base-vs-base is a real Postgres `UNIQUE` constraint (forwarded to the base
model automatically). The new part is a deferred constraint trigger that
also rejects a base row whose value already exists, untouched, in the
source table. It's a write-time snapshot check, not a standing guarantee —
a source row that starts colliding with an already-materialized base row
*after the fact* isn't retroactively caught (same limitation as the
FK-safety trigger).

**Index the source table's constrained column(s) yourself** — the trigger
queries a table this package doesn't own the DDL for. Benchmarked on a
500k-row source table: with an index, an insert runs about **1.2x** a plain
table's native `UniqueConstraint` (~0.07ms vs ~0.06ms). Without one: **~150x**
slower (~10ms) — the `EXISTS` check degrades to a sequential scan.

`condition=` isn't supported — declaring one raises `OverlayConfigurationError`
at class-definition time. A partial constraint's condition would apply
correctly to the native base-table index but the source-vs-base trigger has
no way to honor it, so it would silently check for collisions the condition
should have excluded.

## `OverlayUniqueConstraint` is the only form allowed

An overlay model is queried through a view spanning your base table and the
source table, so uniqueness has to hold across both. Every other way Django
lets you declare it compiles down to a single index or table constraint on your
table alone, which happily accepts a value that already exists in the source.
So they're all rejected at import time, with the replacement line in the error:

| declared as | why |
|---|---|
| `Meta.unique_together` | table constraint: your table only, and takes no predicate |
| `Field(unique=True)` | same |
| plain `UniqueConstraint` | covers your table only |
| `UniqueConstraint(condition=...)` | as above, and the source-side trigger can't apply a condition |
| `CheckConstraint` | fine, not a uniqueness rule |

```
Employee declares uniqueness django_overlay can't honour:

  - Meta.unique_together = ['first_name', 'last_name']
  - email declares unique=True

An overlay model is queried through a view spanning your table and the source
table, so uniqueness has to hold across both. ...

Declare them as OverlayUniqueConstraint in Meta.constraints instead:

    constraints = [
        OverlayUniqueConstraint(fields=["first_name", "last_name"], name="employee_first_name_last_name_uniq"),
        OverlayUniqueConstraint(fields=["email"], name="employee_email_uniq"),
    ]
```

A `OneToOneField` keeps working — it's implicitly unique, and rejecting it
would cost you the singular reverse accessor for a mechanical reason. The base
model stores the `ForeignKey` underneath it (so Django emits no table
constraint) and you declare the matching `OverlayUniqueConstraint` yourself.
Forgetting it is an error, not a silent downgrade.

On a `soft_delete` model every constraint is narrowed to
`WHERE NOT _overlay_deleted` automatically, so you never name that column
yourself — see [DELETION.md](DELETION.md).
