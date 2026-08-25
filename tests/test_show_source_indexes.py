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


def test_the_help_text_is_what_the_command_advertises():
    """Every option's help string, compared rather than ignored.

    Seventeen mutants lived in `add_arguments`: garbled help, dropped help,
    changed defaults. Nothing had ever looked at what `--help` prints, so the
    only part of the parser under test was that the flags existed.
    """
    from django_overlay.management.commands.show_source_indexes import Command

    # argparse wraps to the terminal width, so the text is compared with its
    # whitespace flattened rather than line by line.
    printed = " ".join(Command().create_parser("manage.py", "show_source_indexes").format_help().split())

    assert "--database DATABASE Database alias to introspect." in printed
    assert "--model MODEL Only this model, as app_label.ModelName." in printed
    assert "--missing-only Skip models whose indexes already line up on both sides." in printed


def test_the_database_option_defaults_to_default():
    """The default is what makes the flag optional, and it was free to change."""
    from django_overlay.management.commands.show_source_indexes import Command

    parser = Command().create_parser("manage.py", "show_source_indexes")
    options = vars(parser.parse_args([]))
    assert options["database"] == "default"
    assert options["model"] is None
    assert options["missing_only"] is False

    chosen = vars(parser.parse_args(["--database", "other", "--model", "app.M", "--missing-only"]))
    assert chosen["database"] == "other"
    assert chosen["model"] == "app.M"
    assert chosen["missing_only"] is True


def test_lists_both_sides_for_one_model():
    output = run(model="testapp.Person")

    assert "testapp.Person" in output
    assert "public.testapp_shared_personsource" in output
    assert "source  btree (id) UNIQUE" in output
    assert "base    btree (id) UNIQUE" in output


def test_the_report_for_one_model_is_exactly_this():
    """Line for line, including the blank one that separates models.

    The report was asserted with substrings, which left the UNIQUE suffix, the
    trailing blank line and the whole no-indexes line unchecked -- six mutants
    between them, all in text a person reads.
    """
    output = run(model="testapp.Person")

    assert output == (
        "testapp.Person  public.person  <-  public.testapp_shared_personsource\n"
        "  source  btree (id) UNIQUE  (testapp_shared_personsource_pkey)\n"
        "  base    btree (id) UNIQUE  (person_pkey)\n"
        "\n"
    )


def test_a_non_unique_index_is_listed_without_the_unique_marker(db_cursor):
    """`' UNIQUE' if index['unique'] else ''` -- the else branch is a mutant of
    its own, and unreachable while every index in the report is unique."""
    db_cursor.execute("CREATE INDEX probe_plain_idx ON testapp_shared_personsource (first_name)")
    try:
        output = run(model="testapp.Person")
    finally:
        db_cursor.execute("DROP INDEX probe_plain_idx")

    assert "  source  btree (first_name)  (probe_plain_idx)\n" in output
    assert "btree (first_name) UNIQUE" not in output


def test_a_non_unique_base_index_is_listed_without_the_unique_marker(db_cursor):
    """Same conditional, other side. Each line has its own copy and its own
    mutant, and a test covering one says nothing about the other."""
    db_cursor.execute("CREATE INDEX probe_base_plain_idx ON person (first_name)")

    output = run(model="testapp.Person")

    assert "  base    btree (first_name)  (probe_base_plain_idx)\n" in output
    assert "base    btree (first_name) UNIQUE" not in output


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

    # The whole line. "the source is the big half" was true of a sentence
    # missing its ending, which is where two mutants were living.
    assert (
        "  MISSING on testapp_shared_personsource: btree (age) — the source is the "
        "big half, so this is the expensive gap\n"
    ) in output


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


def test_every_overlay_model_is_covered_by_default():
    output = run()

    assert "testapp.Person" in output
    assert "testapp.Address" in output


def test_an_index_free_source_says_so(monkeypatch):
    monkeypatch.setattr(show_source_indexes, "table_indexes", lambda cursor, schema, table: [])

    output = run(model="testapp.Person")
    assert "  source table has no indexes at all\n" in output


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


def test_a_project_with_no_overlay_models_says_so(monkeypatch):
    """The empty case is still reachable -- a project can install
    django_overlay before declaring anything -- but it stopped being reachable
    *through a sourceless model*, which is how it used to be covered. Since
    every overlay model now has a source, so the only way in is having no
    overlay models at all.
    """
    monkeypatch.setattr(show_source_indexes.Command, "_models", lambda self, label: [])
    out = StringIO()

    call_command("show_source_indexes", stdout=out)

    # Equality, not `in`: mutmut's string mutation wraps the literal as
    # "XX...XX", which still *contains* the original, so a substring assertion
    # passes against it and the mutant survives.
    assert out.getvalue().strip() == "No overlay models with a source table found."
