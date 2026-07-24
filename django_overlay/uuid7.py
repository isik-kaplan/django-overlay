import os
import time
import uuid


def uuid7() -> uuid.UUID:
    """RFC 9562 UUIDv7. Uses the stdlib implementation on Python 3.14+,
    otherwise a manual fallback."""
    native = getattr(uuid, "uuid7", None)
    if native is not None:
        return native()

    ms = time.time_ns() // 1_000_000
    value = bytearray(ms.to_bytes(6, "big") + os.urandom(10))
    value[6] = (value[6] & 0x0F) | 0x70  # version 7
    value[8] = (value[8] & 0x3F) | 0x80  # RFC 4122 variant
    return uuid.UUID(bytes=bytes(value))
