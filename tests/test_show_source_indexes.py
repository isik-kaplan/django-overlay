from io import StringIO

import pytest
from django.core.management import call_command

from django_overlay.introspection import compare_indexes, table_indexes
from django_overlay.management.commands import show_source_indexes


pytestmark = pytest.mark.django_db


def run(**options) -> str:
    out = StringIO()
    call_command("show_source_indexes", stdout=out, **options)
    return out.getvalue()


def test_lists_both_sides_for_one_model():
    output = run(model="testapp.Person")

    assert "testapp.Person" in output
    assert "public.testapp_shared_personsource" in output
    assert "source  btree (id) UNIQUE" in output
    assert "base    btree (id) UNIQUE" in output


def test_reports_an_index_the_base_table_is_missing(db_cursor):
    db_cursor.execute("CREATE INDEX probe_src_idx ON testapp_shared_personsource (first_name, age)")

    output = run(model="testapp.Person")

    assert "MISSING on person: btree (first_name, age)" in output
    assert 'models.Index(fields=["first_name", "age"], name="...")' in output


def test_a_missing_expression_index_is_reported_without_a_django_hint(db_cursor):
    db_cursor.execute("CREATE INDEX probe_expr_idx ON testapp_shared_personsource (lower(first_name))")

    output = run(model="testapp.Person")

    assert "MISSING on person: btree (lower((first_name)::text))" in output
    assert "Meta.indexes" not in output


def test_reports_an_index_the_source_table_is_missing(db_cursor):
    db_cursor.execute("CREATE INDEX probe_base_idx ON person (age)")

    output = run(model="testapp.Person")

    assert "MISSING on testapp_shared_personsource: btree (age)" in output
    assert "the source is the big half" in output


def test_matching_indexes_are_not_reported_as_missing(db_cursor):
    db_cursor.execute("CREATE INDEX probe_src_idx ON testapp_shared_personsource (age)")
    db_cursor.execute("CREATE INDEX probe_base_idx ON person (age)")

    output = run(model="testapp.Person")

    assert "MISSING" not in output


def test_missing_only_skips_models_that_line_up():
    output = run(model="testapp.Person", missing_only=True)

    assert output == ""


def test_missing_only_still_reports_a_mismatch(db_cursor):
    db_cursor.execute("CREATE INDEX probe_src_idx ON testapp_shared_personsource (age)")

    output = run(model="testapp.Person", missing_only=True)

    assert "MISSING on person" in output


def test_a_model_without_a_source_is_skipped():
    assert run(model="testapp.MetaTest") == "No overlay models with a source table found.\n"


def test_every_overlay_model_is_covered_by_default():
    output = run()

    assert "testapp.Person" in output
    assert "testapp.Address" in output


def test_an_index_free_source_says_so(monkeypatch):
    monkeypatch.setattr(show_source_indexes, "table_indexes", lambda cursor, schema, table: [])

    assert "source table has no indexes at all" in run(model="testapp.Person")


@pytest.mark.parametrize(
    "shape",
    [
        "gin (search_vector)",  # not btree
        "btree (lower(first_name))",  # expression
        "btree (first_name varchar_pattern_ops)",  # opclass
    ],
)
def test_no_django_hint_for_an_index_we_cannot_faithfully_translate(shape):
    assert show_source_indexes._django_index_hint(shape) is None


def test_django_hint_for_a_plain_btree():
    assert show_source_indexes._django_index_hint("btree (a, b)") == 'models.Index(fields=["a", "b"], name="...")'


def test_table_indexes_reads_shape_and_uniqueness(db_cursor):
    indexes = table_indexes(db_cursor, "public", "person")

    assert {"name": "person_pkey", "unique": True, "shape": "btree (id)"} in indexes


def test_compare_indexes_matches_on_shape_not_name():
    source = [{"name": "src_a", "shape": "btree (a)", "unique": False}]
    base = [{"name": "base_a", "shape": "btree (a)", "unique": False}]

    assert compare_indexes(source, base) == ([], [])
