import uuid
from unittest.mock import patch

import pytest

from django_overlay.uuid7 import uuid7


@pytest.fixture(autouse=True)
def _fresh_monotonic_state():
    """Reset the fallback's module-level clock state around every test.

    _last_ms and _counter are globals, so a test that pushes the timestamp into
    the future changes what every later test sees. The rollover test rolls about
    three thousand milliseconds forward, which silently disabled the re-seed
    path for anything after it using a smaller timestamp -- a test asserting
    that seeds vary was quietly asserting that a counter increments.
    """
    from django_overlay import uuid7 as module

    module._last_ms, module._counter = -1, 0
    yield
    module._last_ms, module._counter = -1, 0


def test_uuid7_returns_a_version_7_uuid():
    value = uuid7()
    assert isinstance(value, uuid.UUID)
    assert value.version == 7


def test_uuid7_sets_the_rfc4122_variant():
    value = uuid7()
    assert value.variant == uuid.RFC_4122


def test_consecutive_uuid7_calls_have_non_decreasing_timestamp_prefixes():
    # Comparing the full int would be flaky: same-millisecond calls share a
    # timestamp prefix but have unordered random tails.
    first = uuid7()
    second = uuid7()
    assert (first.int >> 80) <= (second.int >> 80)


def test_uuid7_uses_the_stdlib_implementation_when_available():
    sentinel = uuid.uuid4()
    with patch("uuid.uuid7", return_value=sentinel, create=True):
        assert uuid7() is sentinel


def test_uuid7_falls_back_when_the_stdlib_implementation_is_unavailable():
    with patch("uuid.uuid7", None, create=True):
        value = uuid7()
    assert isinstance(value, uuid.UUID)
    assert value.version == 7


# Monotonicity. Two ids from the same millisecond used to order by their random
# tails, which is the one property a v7 id is chosen for.


def test_the_fallback_is_monotonic_within_a_millisecond():
    with patch("uuid.uuid7", None, create=True):
        values = [uuid7() for _ in range(2000)]

    assert values == sorted(values), "ids must sort in the order they were generated"
    assert len(set(values)) == len(values)


def test_the_fallback_stays_monotonic_when_the_clock_goes_backwards():
    with patch("uuid.uuid7", None, create=True):
        with patch("time.time_ns", side_effect=[5_000_000_000, 4_000_000_000, 4_000_000_000]):
            first, second, third = uuid7(), uuid7(), uuid7()

    assert first < second < third


def test_the_fallback_borrows_the_next_millisecond_when_the_counter_runs_out():
    """More than 2048 ids inside one millisecond. The timestamp goes briefly
    ahead of the clock rather than letting two ids collide or invert."""
    with patch("uuid.uuid7", None, create=True):
        with patch("time.time_ns", return_value=5_000_000_000):
            values = [uuid7() for _ in range(5000)]

    assert values == sorted(values)
    assert len(set(values)) == len(values)
    assert (values[-1].int >> 80) > (values[0].int >> 80), "it moved into the next millisecond"


def test_the_counter_seeds_randomly_each_millisecond():
    """Seeded rather than reset to zero, so ids from separate processes in the
    same millisecond don't line up predictably."""
    with patch("uuid.uuid7", None, create=True):
        seeds = set()
        for tick in range(20):
            with patch("time.time_ns", return_value=(6_000 + tick) * 1_000_000):
                seeds.add((uuid7().int >> 64) & 0x0FFF)

    assert len(seeds) > 1


def test_the_timestamp_is_the_clock_in_whole_milliseconds():
    """`time_ns() // 1_000_000`, and the divisor has to be exactly that.

    A divisor one off produces a timestamp that drifts from the clock by a
    factor no test noticed, because everything else here only compares ids to
    each other. This compares one to the time it was generated at.
    """
    with patch("uuid.uuid7", None, create=True):
        with patch("time.time_ns", return_value=1_700_000_123_456_789):
            value = uuid7()

    assert (value.int >> 80) == 1_700_000_123_456_789 // 1_000_000


def test_the_counter_seed_stays_in_the_low_half():
    """Seeded in the low half so there is room to count up: 2048 ids per
    millisecond before the rollover path is needed.

    `& _COUNTER_SEED_MASK` is what bounds it. With `|` in place of `&` the seed
    is forced high instead, leaving almost no headroom -- and nothing noticed,
    because the rollover path handles the overflow correctly either way.
    """
    from django_overlay.uuid7 import _COUNTER_SEED_MASK

    with patch("uuid.uuid7", None, create=True):
        seeds = []
        for tick in range(40):
            with patch("time.time_ns", return_value=(7_000 + tick) * 1_000_000):
                seeds.append((uuid7().int >> 64) & 0x0FFF)

    assert all(seed <= _COUNTER_SEED_MASK for seed in seeds), (
        f"every seed must fit in the low half (<= {_COUNTER_SEED_MASK}), got {sorted(seeds)}"
    )
    assert max(seeds) > _COUNTER_SEED_MASK // 4, "the seed should use the range it has"


def test_the_rollover_borrows_exactly_one_millisecond_and_restarts_at_zero():
    """The two constants in the rollover path, each observable on its own.

    Borrowing two milliseconds instead of one skips a millisecond the clock
    will catch up with anyway; restarting the counter at one instead of zero
    quietly loses an id's worth of headroom. Both survived while the only
    assertions were that the ids sort and do not collide.
    """
    from django_overlay import uuid7 as module

    with patch("uuid.uuid7", None, create=True):
        with patch("time.time_ns", return_value=8_000 * 1_000_000):
            # Exhaust the counter, then take one more to force the rollover.
            module._last_ms = 8_000
            module._counter = module._COUNTER_MAX
            rolled = uuid7()

    assert (rolled.int >> 80) == 8_001, "it should borrow exactly one millisecond"
    assert ((rolled.int >> 64) & 0x0FFF) == 0, "the counter should restart at zero"


def test_the_variant_byte_keeps_its_own_random_bits():
    """Byte 8 carries the variant in its top two bits and randomness below.

    Both ways of getting that wrong still produce a valid RFC 4122 variant, so
    every assertion about `.variant` passes: reading byte 9 instead ties the two
    bytes together forever, and OR-ing 0x81 instead of 0x80 pins the lowest bit
    high. Neither is visible in a single id, which is why both survived -- it
    takes a sample.
    """
    with patch("uuid.uuid7", None, create=True):
        sample = [uuid7().bytes for _ in range(40)]

    assert any(value[8] & 0x3F != value[9] & 0x3F for value in sample), (
        "byte 8 must carry its own randomness, not a copy of byte 9"
    )
    assert any(value[8] & 0x01 == 0 for value in sample), "no bit below the variant may be forced high"
