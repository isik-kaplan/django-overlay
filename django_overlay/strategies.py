import enum
import uuid

from django.db import models

from ._templating import render
from .uuid7 import uuid7


class Strategy(enum.Enum):
    """How an organically-created row gets an id that can never collide
    with an untouched source row."""

    # Assumes the source table's id column is a non-negative integer.
    NEGATIVE_ID = "negative_id"
    UUID4 = "uuid4"
    UUID7 = "uuid7"
    UUID7_POLYFILL = "uuid7_polyfill"


_FIELD_FACTORIES = {
    Strategy.UUID4: lambda: models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False),
    Strategy.UUID7: lambda: models.UUIDField(primary_key=True, default=uuid7, editable=False),
    Strategy.UUID7_POLYFILL: lambda: models.UUIDField(primary_key=True, default=uuid7, editable=False),
}

# UUID7 needs Postgres 18+ for native uuidv7() — use UUID7_POLYFILL otherwise.
_PK_DEFAULT_TEMPLATES = {
    Strategy.UUID4: "pk_defaults/uuid4.sql.j2",
    Strategy.UUID7: "pk_defaults/uuid7.sql.j2",
    Strategy.UUID7_POLYFILL: "pk_defaults/uuid7_polyfill.sql.j2",
}


def default_id_field(strategy: Strategy) -> models.Field | None:
    factory = _FIELD_FACTORIES.get(strategy)
    return factory() if factory else None


def default_pk_sql(strategy: Strategy) -> str | None:
    template_name = _PK_DEFAULT_TEMPLATES.get(strategy)
    return render(template_name) if template_name else None


def negates_source_ids(strategy: Strategy) -> bool:
    return strategy is Strategy.NEGATIVE_ID
