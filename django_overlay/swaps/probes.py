"""The context a check runs in, and the catalogue reads every check shares.

Nothing here decides anything. `_Probe` resolves the model, the two sources
and the schema once, so a check is a function of one argument; the rest read
the catalogue and are used by more than one check.
"""

from dataclasses import dataclass

from .._templating import render
from ..sources import SourceTable
from ..strategies import negates_source_ids


@dataclass(frozen=True)
class _Probe:
    """Everything a check needs, resolved once. The model is the view model --
    the one application code holds and the one get_source() lives on."""

    cursor: object
    model: type
    tenant_schema: str
    current: SourceTable
    candidate: SourceTable
    identity_columns: tuple[str, ...]
    min_row_ratio: float
    using: str

    @property
    def base_model(self):
        return self.model._base_model

    @property
    def base_table(self) -> str:
        return self.base_model._meta.db_table

    @property
    def pk_column(self) -> str:
        return self.model._meta.pk.column

    @property
    def negate(self) -> bool:
        return negates_source_ids(self.model._overlay_meta.strategy)

    @property
    def soft_delete(self) -> bool:
        return self.model._overlay_meta.soft_delete

    def scalar_row(self, template: str, **context):
        self.cursor.execute(render(f"swaps/{template}", **context))
        return self.cursor.fetchone()

    def count(self, template: str, **context) -> int:
        return self.scalar_row(template, **context)[0]


def _relation_exists(cursor, source: SourceTable) -> bool:
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [f'"{source.schema}"."{source.table}"'])
    return cursor.fetchone()[0]


def _column_types(cursor, source: SourceTable) -> dict:
    cursor.execute(render("swaps/column_types.sql.j2"), [source.schema, source.table])
    return dict(cursor.fetchall())


def _required_columns(model, source: SourceTable) -> list[str]:
    """The columns the view's source branch selects: every declared column,
    with the model's primary key standing in for the source's id column.

    See view.sql.j2's select_list macro -- the pk is the one column read under
    a different name (and possibly negated), and every other one has to be
    there under the name the model gave it."""
    pk_column = model._meta.pk.column
    columns = [source.id_column]
    columns += [f.column for f in model._meta.fields if f.column != pk_column]
    return columns


def _estimated_rows(cursor, source: SourceTable):
    cursor.execute(
        render("swaps/estimated_rows.sql.j2"),
        {"relation": f'"{source.schema}"."{source.table}"'},
    )
    return cursor.fetchone()
