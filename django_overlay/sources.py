from dataclasses import dataclass


@dataclass(frozen=True)
class SourceTable:
    """A model's read-only source table. `extra_where` is spliced directly
    into the view's CREATE VIEW text (a view has no bind parameters) — a
    hardcoded literal is fine, but a value built from data (e.g. a
    subscription-tier filter) must be SQL-quoted yourself first (e.g.
    `psycopg.sql.Literal(value).as_string(connection)`), never
    f-string-interpolated."""

    schema: str
    table: str
    id_column: str = "id"
    extra_where: str = ""

    @property
    def qualified_name(self) -> str:
        return f'"{self.schema}"."{self.table}"'
