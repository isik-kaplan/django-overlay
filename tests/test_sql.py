from django_overlay import sql
from django_overlay.sources import SourceTable
from django_overlay.strategies import Strategy


def test_insert_sql_defaults_to_nextval_for_the_negative_id_strategy():
    rendered = sql.build_instead_of_insert_sql(
        "person_view", "public", "person", ["id", "first_name"], "id", "person_id_seq"
    )
    assert 'nextval(\'"public"."person_id_seq"\')' in rendered
    assert "gen_random_uuid" not in rendered


def test_insert_sql_defaults_to_gen_random_uuid_for_the_uuid4_strategy():
    rendered = sql.build_instead_of_insert_sql(
        "widget_view", "public", "widget", ["id", "label"], "id", "widget_id_seq", strategy=Strategy.UUID4
    )
    assert "gen_random_uuid()" in rendered
    assert "nextval" not in rendered


def test_insert_sql_defaults_to_uuidv7_for_the_uuid7_strategy():
    rendered = sql.build_instead_of_insert_sql(
        "gizmo_view", "public", "gizmo", ["id", "label"], "id", "gizmo_id_seq", strategy=Strategy.UUID7
    )
    assert "uuidv7()" in rendered


def test_insert_sql_defaults_to_the_polyfill_for_the_uuid7_polyfill_strategy():
    rendered = sql.build_instead_of_insert_sql(
        "gizmo_view",
        "public",
        "gizmo",
        ["id", "label"],
        "id",
        "gizmo_id_seq",
        strategy=Strategy.UUID7_POLYFILL,
    )
    assert "gen_random_uuid()" in rendered
    assert "uuidv7()" not in rendered


def test_constraint_trigger_skips_the_check_when_the_column_is_unchanged():
    rendered = sql.build_constraint_trigger_sql(
        "overlayfk_note_person_id",
        "public",
        "note",
        "person_id",
        [{"schema": "public", "table": "person", "id_column": "id", "negate": False}],
    )
    assert 'TG_OP = \'INSERT\' OR NEW."person_id" IS DISTINCT FROM OLD."person_id"' in rendered


def test_unique_constraint_trigger_skips_the_check_when_no_constrained_column_changed():
    rendered = sql.build_unique_constraint_trigger_sql(
        "overlayunique_uniquetest_x",
        "public",
        "uniquetest",
        ["ssn"],
        SourceTable(schema="public", table="uniquetestsource"),
        "id",
    )
    assert "TG_OP = 'INSERT' OR" in rendered
    assert 'NEW."ssn" IS DISTINCT FROM OLD."ssn"' in rendered


# Default arguments no caller omits are still part of the contract, and were
# where three mutants lived: every call site passes these explicitly, so the
# defaults themselves were never rendered.
def test_the_referencing_pk_defaults_to_id():
    rendered = sql.build_constraint_trigger_sql(
        "overlayfk_note_person_id",
        "public",
        "note",
        "person_id",
        [{"schema": "public", "table": "person", "id_column": "id", "negate": False}],
    )
    explicit = sql.build_constraint_trigger_sql(
        "overlayfk_note_person_id",
        "public",
        "note",
        "person_id",
        [{"schema": "public", "table": "person", "id_column": "id", "negate": False}],
        referencing_pk="id",
    )
    assert rendered == explicit
    assert '"id"' in rendered


def test_the_target_pk_defaults_to_id():
    rendered = sql.build_referenced_row_trigger_sql(
        "overlayref_person_note_person_id",
        "public",
        "note",
        "person_id",
        "person",
        "person_view",
    )
    explicit = sql.build_referenced_row_trigger_sql(
        "overlayref_person_note_person_id",
        "public",
        "note",
        "person_id",
        "person",
        "person_view",
        target_pk="id",
    )
    assert rendered == explicit
    assert '"id"' in rendered


def test_soft_delete_defaults_to_off_in_the_unique_trigger():
    """The default has to be the safe one: a trigger that assumes tombstones
    on a table without them would filter on a column that is not there."""
    rendered = sql.build_unique_constraint_trigger_sql(
        "overlayunique_uniquetest_x",
        "public",
        "uniquetest",
        ["ssn"],
        SourceTable(schema="public", table="uniquetestsource"),
        "id",
    )
    assert "_overlay_deleted" not in rendered
    assert rendered == sql.build_unique_constraint_trigger_sql(
        "overlayunique_uniquetest_x",
        "public",
        "uniquetest",
        ["ssn"],
        SourceTable(schema="public", table="uniquetestsource"),
        "id",
        soft_delete=False,
    )


def test_pk_default_sql_overrides_the_strategy_default():
    rendered = sql.build_instead_of_insert_sql(
        "widget_view",
        "public",
        "widget",
        ["id", "label"],
        "id",
        "widget_id_seq",
        strategy=Strategy.UUID4,
        pk_default_sql="testapp_custom_uuid()",
    )
    assert "testapp_custom_uuid()" in rendered
    assert "gen_random_uuid" not in rendered


# The four builders below are called from exactly one place -- sync_view -- and
# it passes every argument, so their defaults had never been rendered once.
# Thirteen mutants lived there, and phase two confirmed all thirteen against
# the whole suite. Same shape as the three trigger defaults above: render with
# the defaults omitted, render again with them written out, compare. A mutated
# default changes only the first of the two.
def test_the_view_defaults_to_an_overridable_hard_deleting_table_keyed_on_id():
    source = SourceTable(schema="public", table="personsource")
    columns = ["id", "first_name"]
    rendered = sql.build_view_sql("person_view", "public", "person", source, columns)
    assert rendered == sql.build_view_sql(
        "person_view",
        "public",
        "person",
        source,
        columns,
        pk_column="id",
        strategy=Strategy.NEGATIVE_ID,
        soft_delete=False,
        overridable=True,
    )
    # What those defaults mean, spelled out. The negated source id is the
    # NEGATIVE_ID strategy showing through, and the anti-join is the full one
    # rather than the narrowed tombstone form -- overridable = True with no
    # soft delete.
    assert '"id" AS "id"' in rendered
    assert '-"id" AS "id"' in rendered
    assert "_overlay_deleted" not in rendered
    assert 'NOT EXISTS (SELECT 1 FROM "public"."person" AS overlay_base' in rendered


def test_the_insert_trigger_defaults_to_overridable_with_no_tombstone_column():
    """`source` is passed here rather than left to default, because the
    template guards the non-overridable branch on `not overridable and source`
    -- with no source the two values of `overridable` render identically and
    nothing could tell them apart."""
    columns = ["id", "first_name"]
    source = SourceTable(schema="public", table="personsource")
    rendered = sql.build_instead_of_insert_sql(
        "person_view", "public", "person", columns, "id", "person_id_seq", source=source
    )
    assert rendered == sql.build_instead_of_insert_sql(
        "person_view",
        "public",
        "person",
        columns,
        "id",
        "person_id_seq",
        strategy=Strategy.NEGATIVE_ID,
        pk_default_sql=None,
        soft_delete=False,
        source=source,
        overridable=True,
    )
    assert "_overlay_deleted" not in rendered
    assert "overridable = False" not in rendered


def test_the_update_trigger_defaults_to_id_and_an_overridable_model():
    columns = ["id", "first_name"]
    rendered = sql.build_instead_of_update_sql("person_view", "public", "person", columns)
    assert rendered == sql.build_instead_of_update_sql(
        "person_view",
        "public",
        "person",
        columns,
        pk_column="id",
        soft_delete=False,
        overridable=True,
    )
    assert 'WHERE "id" = OLD."id"' in rendered
    assert "_overlay_deleted" not in rendered
    assert "overridable = False" not in rendered


def test_the_delete_trigger_defaults_to_id_and_a_hard_delete():
    rendered = sql.build_instead_of_delete_sql("person_view", "public", "person")
    assert rendered == sql.build_instead_of_delete_sql(
        "person_view",
        "public",
        "person",
        pk_column="id",
        columns=None,
        soft_delete=False,
    )
    # soft_delete picks the template, so the default decides whether a DELETE
    # removes the row or writes a tombstone.
    assert 'DELETE FROM "public"."person" WHERE "id" = OLD."id"' in rendered
    assert "_overlay_deleted" not in rendered
