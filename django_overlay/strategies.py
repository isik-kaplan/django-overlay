import enum
import uuid

from django.db import models

from .uuid7 import uuid7


class Strategy(enum.Enum):
    """How an organically-created row gets an id that can never collide
    with an untouched source row."""

    NEGATIVE_ID = "negative_id"
    UUID4 = "uuid4"
    UUID7 = "uuid7"
    UUID7_POLYFILL = "uuid7_polyfill"


# Extension-free stand-in for Postgres 18's native uuidv7(): a 48-bit
# millisecond timestamp plus RFC 9562 version/variant nibbles, with random
# filler drawn from one gen_random_uuid() call (reusing its own valid
# variant nibble at hex position 17; avoiding its version nibble at
# position 13, always '4', so it doesn't bias the random bits).
_UUID7_POLYFILL_SQL = """(SELECT (
    lpad(to_hex(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint), 12, '0') ||
    '7' ||
    substr(src, 1, 3) ||
    substr(src, 17, 1) ||
    substr(src, 4, 1) ||
    substr(src, 18, 14)
)::uuid FROM (SELECT replace(gen_random_uuid()::text, '-', '') AS src) AS s)"""

_FIELD_FACTORIES = {
    Strategy.UUID4: lambda: models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False),
    Strategy.UUID7: lambda: models.UUIDField(primary_key=True, default=uuid7, editable=False),
    Strategy.UUID7_POLYFILL: lambda: models.UUIDField(primary_key=True, default=uuid7, editable=False),
}

_DEFAULT_PK_SQL = {
    Strategy.UUID4: "gen_random_uuid()",
    # Needs Postgres 18+: PL/pgSQL resolves every function name in a
    # trigger body to compile it, so if uuidv7() doesn't exist, every
    # insert fails, not just id-less ones. Use UUID7_POLYFILL otherwise.
    Strategy.UUID7: "uuidv7()",
    Strategy.UUID7_POLYFILL: _UUID7_POLYFILL_SQL,
}


def default_id_field(strategy: Strategy) -> models.Field | None:
    factory = _FIELD_FACTORIES.get(strategy)
    return factory() if factory else None


def default_pk_sql(strategy: Strategy) -> str | None:
    return _DEFAULT_PK_SQL.get(strategy)


def negates_source_ids(strategy: Strategy) -> bool:
    return strategy is Strategy.NEGATIVE_ID
