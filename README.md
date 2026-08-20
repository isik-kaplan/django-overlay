# django-overlay

A writable Django model layered on top of a read-only table you don't own —
one model to query, no `COALESCE`/`FULL OUTER JOIN` at read time.

## The problem

You have a big shared read-only table (a vendor import, a third-party
dataset) and a small table of tenant edits on top of it. You want one Django model to
query, writes landing only in your own table, an edit that copies the row
over on first touch, and a foreign key that can still point at it even
though Postgres can't put a real FK on a view.

This is [OverlayFS](https://docs.kernel.org/filesystems/overlayfs.html)
applied to Postgres: a writable "upper" table merged with one read-only
"lower" table into one "merged" view. Each overlay model has exactly one
source table, though which physical table that is can be resolved per
tenant — a single, tenant-scoped source, not a union of several.

```python
from django.db import models

from django_overlay.models import OverlayMeta, OverlayModel
from django_overlay.sources import SourceTable


class Person(OverlayModel):
    first_name = models.CharField(max_length=100)

    class OverlayMeta(OverlayMeta):
        @staticmethod
        def get_source():
            return SourceTable(schema="external_source", table="people")


Person.objects.create(first_name="Jane")  # goes straight into the writable table
```

See [docs/reference/COMPATIBILITY.md](docs/reference/COMPATIBILITY.md) for measured
tables of exactly where this matches a plain Django model and where it doesn't,
and [docs/INDEX.md](docs/INDEX.md) for everything else — usage, how it
works, ids, uniqueness, deletion, migrations, and running the tests.

From a source checkout, `uv run django-overlay benchmark` measures all of this
against plain tables holding identical rows — see
[docs/development/BENCHMARKS.md](docs/development/BENCHMARKS.md).
