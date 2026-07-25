import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from django_overlay._templating import _qi


pytestmark = pytest.mark.django_db


@given(name=st.text(min_size=1, max_size=20).filter(lambda s: "\x00" not in s and len(s.encode()) <= 63))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_qi_round_trips_any_identifier_through_postgres(db_cursor, name):
    db_cursor.execute(f"SELECT 1 AS {_qi(name)}")
    assert db_cursor.description[0].name == name
