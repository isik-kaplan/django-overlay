import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from django_overlay._templating import _qi, _sql_literal


pytestmark = pytest.mark.django_db


@given(name=st.text(min_size=1, max_size=20).filter(lambda s: "\x00" not in s and len(s.encode()) <= 63))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_qi_round_trips_any_identifier_through_postgres(db_cursor, name):
    db_cursor.execute(f"SELECT 1 AS {_qi(name)}")
    assert db_cursor.description[0].name == name


def test_a_quote_inside_a_literal_is_doubled():
    """The whole job of the filter, and no test ever passed it a quote.

    Both mutants of `.replace("'", "''")` survived because every value it was
    given was quote-free, where replacing nothing with anything is the same as
    doing nothing.
    """
    assert _sql_literal("O'Brien") == "'O''Brien'"
    assert _sql_literal("''") == "'" + "''''" + "'"
    assert _sql_literal("plain") == "'plain'"


def test_a_literal_is_quoted_even_when_it_is_not_a_string():
    assert _sql_literal(42) == "'42'"
