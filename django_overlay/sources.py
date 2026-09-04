from dataclasses import dataclass


@dataclass(frozen=True)
class SourceTable:
    """A model's read-only source table. `extra_where` is spliced directly
    into the view's SQL — a hardcoded literal is fine, but a data-derived
    value must be quoted yourself first (e.g. `psycopg.sql.Literal`), never
    f-string'd in.

    `partition_key` names the column the source is declaratively partitioned
    on, when it is. Purely a declaration: this package never creates, attaches
    or detaches a partition, and the view reads the parent table exactly as it
    reads an ordinary one. What it buys is that every probe this package
    generates against the source can carry the key, so Postgres can prune to
    one partition instead of scanning all of them.

    That distinction is worth stating precisely, because it is the whole reason
    the field exists. A partitioned parent is transparent to *correctness* --
    `SELECT ... FROM people WHERE email = %s` returns the right rows either way
    -- and opaque to *cost*: without the key in the predicate the plan is an
    Append over every partition. Ordinary application queries supply the key
    themselves. The triggers this package writes cannot, because nobody is
    there to write them, so they are told once, here.

    Left None -- the default -- every template renders exactly the SQL it
    rendered before this existed, byte for byte, so nothing churns for a
    project that does not partition. Setting it on an existing model rewrites
    trigger bodies and therefore needs a resync, the same as `overridable`.

    The column has to be one the model also has a field for, since the view
    selects it from both branches under the same name. `manage.py check`
    enforces that rather than leaving it to a runtime error inside a trigger.
    """

    schema: str
    table: str
    id_column: str = "id"
    extra_where: str = ""
    partition_key: str | None = None
