import os
import threading
import time
import uuid


# RFC 9562 §6.2 "method 3": the 12 bits the spec leaves free right after the
# timestamp carry a counter, seeded randomly once per millisecond and
# incremented for every id issued inside it. Without it, two ids from the same
# millisecond order by their random tails — measured at 981 out-of-order pairs
# in 2000 — which defeats the one property a v7 id is chosen for.
#
# Seeded in the low half so there is room to count up: 2048 ids per millisecond
# before the rollover path below is needed.
_COUNTER_BITS = 12
_COUNTER_MAX = (1 << _COUNTER_BITS) - 1
_COUNTER_SEED_MASK = _COUNTER_MAX >> 1

_lock = threading.Lock()
_last_ms = -1
_counter = 0


def _next_timestamp_and_counter() -> tuple[int, int]:
    """The (millisecond, counter) pair for the next id, never repeating and
    never going backwards — including when the system clock does."""
    global _last_ms, _counter

    now = time.time_ns() // 1_000_000
    with _lock:
        if now > _last_ms:
            _last_ms = now
            _counter = int.from_bytes(os.urandom(2), "big") & _COUNTER_SEED_MASK
        elif _counter < _COUNTER_MAX:
            # Same millisecond, or the clock moved backwards — either way the
            # counter is what keeps this id above the last one.
            _counter += 1
        else:
            # More than 2048 ids inside one millisecond. Borrowing from the
            # next millisecond keeps them ordered; the timestamp is briefly
            # ahead of the clock, which the spec allows and which the clock
            # catches up with on its own.
            _last_ms += 1
            _counter = 0
        return _last_ms, _counter


def uuid7() -> uuid.UUID:
    """RFC 9562 UUIDv7. Uses the stdlib implementation on Python 3.14+,
    otherwise a manual fallback.

    Both are monotonic: ids issued in the same millisecond still sort in the
    order they were generated, so `ORDER BY id` means what a reader expects it
    to mean."""
    native = getattr(uuid, "uuid7", None)
    if native is not None:
        return native()

    ms, counter = _next_timestamp_and_counter()
    value = bytearray(ms.to_bytes(6, "big") + os.urandom(10))
    value[6] = 0x70 | (counter >> 8)  # version 7, then the counter's high nibble
    value[7] = counter & 0xFF
    value[8] = (value[8] & 0x3F) | 0x80  # RFC 4122 variant
    return uuid.UUID(bytes=bytes(value))
