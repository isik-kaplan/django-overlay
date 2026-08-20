import uuid
from unittest.mock import patch

from django_overlay.uuid7 import uuid7


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
