import jinja2
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from django_overlay._templating import _TEMPLATE_NAMES, _qi, _sql_literal, render


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


def test_a_template_name_spelled_in_the_wrong_case_is_not_found():
    """Jinja resolves a template by opening the file, so on a case-insensitive
    filesystem this renders happily and on the Linux box that runs CI it does
    not. Checking the name against the real list first makes the same call mean
    the same thing on both."""
    with pytest.raises(jinja2.TemplateNotFound) as raised:
        render("VIEW/VIEW.SQL.J2")

    assert "VIEW/VIEW.SQL.J2" in str(raised.value)


def test_a_template_that_does_not_exist_is_still_not_found():
    """The check must not become the only thing that answers: a name that is
    wrong in any other way has to fail the same way it always did."""
    with pytest.raises(jinja2.TemplateNotFound):
        render("view/no_such_template.sql.j2")


def test_the_real_names_are_the_ones_that_render():
    """And the list is the list, not an empty set that refuses everything: the
    templates this package ships are all reachable by the name it calls them."""
    assert "view/view.sql.j2" in _TEMPLATE_NAMES
    assert render("pk_defaults/sequence_nextval.sql.j2", tenant_schema="s", pk_sequence="q")
