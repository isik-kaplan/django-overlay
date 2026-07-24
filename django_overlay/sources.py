from dataclasses import dataclass


@dataclass(frozen=True)
class SourceTable:
    """A model's read-only source table. `extra_where` is spliced directly
    into the view's SQL — a hardcoded literal is fine, but a data-derived
    value must be quoted yourself first (e.g. `psycopg.sql.Literal`), never
    f-string'd in."""

    schema: str
    table: str
    id_column: str = "id"
    extra_where: str = ""
