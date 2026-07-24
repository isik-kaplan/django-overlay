import uuid

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
