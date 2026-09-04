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


# ------------------------------------------------------------ partitioned sources
#
# A partitioned parent is transparent to correctness and opaque to cost: the
# same probe returns the same rows either way, and without the partition key in
# its predicate Postgres plans an Append over every partition. Application
# queries carry the key themselves. These triggers cannot, so the source is
# asked to declare it once -- see SourceTable.partition_key.
#
# The pairing that matters in every test below is the negative one. `None` has
# to leave the SQL exactly as it was, or the feature churns every trigger body
# in every project that does not partition.

PARTITIONED = SourceTable(schema="external", table="people", partition_key="state")
UNPARTITIONED = SourceTable(schema="external", table="people")


def test_the_insert_trigger_prunes_the_collision_check_on_a_partitioned_source():
    """The non-overridable collision check probes the source by pk. NEW is a
    view row, so the key is already in scope and needs no declaration."""
    rendered = sql.build_instead_of_insert_sql(
        "person_view",
        "public",
        "person",
        ["id", "state", "first_name"],
        "id",
        "person_id_seq",
        source=PARTITIONED,
        overridable=False,
    )
    assert '"state" = NEW."state"' in rendered


def test_the_insert_trigger_is_unchanged_without_a_partition_key():
    partitioned, plain = (
        sql.build_instead_of_insert_sql(
            "person_view",
            "public",
            "person",
            ["id", "state", "first_name"],
            "id",
            "person_id_seq",
            source=source,
            overridable=False,
        )
        for source in (PARTITIONED, UNPARTITIONED)
    )
    assert '"state" = NEW."state"' not in plain
    assert plain != partitioned


def test_the_soft_delete_trigger_prunes_its_source_lookup():
    """One indexed lookup per deleted row is the documented cost. Unpruned on a
    partitioned source it is one lookup per partition per deleted row."""
    rendered = sql.build_instead_of_delete_sql(
        "person_view",
        "public",
        "person",
        "id",
        ["id", "state", "first_name"],
        soft_delete=True,
        source=PARTITIONED,
    )
    assert '"state" = OLD."state"' in rendered


def test_the_soft_delete_trigger_is_unchanged_without_a_partition_key():
    rendered = sql.build_instead_of_delete_sql(
        "person_view",
        "public",
        "person",
        "id",
        ["id", "state", "first_name"],
        soft_delete=True,
        source=UNPARTITIONED,
    )
    assert '"state" = OLD."state"' not in rendered


def test_the_referenced_row_trigger_prunes_the_view_lookup():
    """It fires on the target's own base table, so OLD is a target row and the
    key needs no declaration from the referencing side."""
    rendered = sql.build_referenced_row_trigger_sql(
        "overlayfkdel_note_person_id",
        "public",
        "note",
        "person_id",
        "person",
        "person_view",
        partition_key="state",
    )
    assert '"state" = OLD."state"' in rendered


def test_the_referenced_row_trigger_is_unchanged_without_a_partition_key():
    rendered = sql.build_referenced_row_trigger_sql(
        "overlayfkdel_note_person_id", "public", "note", "person_id", "person", "person_view"
    )
    assert '"state" = OLD."state"' not in rendered


def test_the_fk_trigger_prunes_only_the_source_target():
    """The base table is an ordinary unpartitioned table. A key predicate on it
    would filter correctly and prune nothing, so it goes on the source entry
    alone."""
    targets = [
        {"schema": "public", "table": "person", "id_column": "id", "negate": False, "soft_delete": False},
        {
            "schema": "external",
            "table": "people",
            "id_column": "id",
            "negate": False,
            "soft_delete": False,
            "partition": {"column": "state", "local_column": "person_state"},
        },
    ]
    rendered = sql.build_constraint_trigger_sql("overlayfk_note_person_id", "public", "note", "person_id", targets)

    # Both branches are emitted inline on one line, so split on the EXISTS
    # boundaries rather than on newlines -- otherwise "the base branch" is the
    # same string as "the source branch" and the negative assertion is vacuous.
    branches = rendered.split("EXISTS (SELECT 1 FROM")
    source_branch = [branch for branch in branches if '"people" WHERE' in branch][0]
    base_branch = [branch for branch in branches if '"person" WHERE' in branch][0]

    assert '"state" = NEW."person_state"' in source_branch
    assert '"state" = NEW.' not in base_branch


def test_the_fk_trigger_is_unchanged_when_no_partition_column_is_declared():
    """Correct but unpruned, which is the documented fallback: only the caller
    knows whether a referencing row shares its target's partition."""
    targets = [
        {"schema": "public", "table": "person", "id_column": "id", "negate": False, "soft_delete": False},
        {
            "schema": "external",
            "table": "people",
            "id_column": "id",
            "negate": False,
            "soft_delete": False,
            "partition": None,
        },
    ]
    rendered = sql.build_constraint_trigger_sql("overlayfk_note_person_id", "public", "note", "person_id", targets)
    assert '"state" =' not in rendered


def test_the_unique_trigger_prunes_from_its_fields_alone():
    """No new argument, and none needed. The template already emits one
    `src.<col> = NEW.<col>` per constrained field, so naming the partition key
    in `fields` *is* the declaration -- and since the key is what decides which
    partition a row lives in, a match on (state, email) can only ever be in
    that state's partition. Pruning is provably a no-op on the result.

    Which is also why a global unique needs no opt-out: leave the key out and
    the probe fans out, because it genuinely has to."""
    scoped = sql.build_unique_constraint_trigger_sql(
        "overlayunique_person_email", "public", "person", ["state", "email"], PARTITIONED, "id"
    )
    global_unique = sql.build_unique_constraint_trigger_sql(
        "overlayunique_person_email", "public", "person", ["email"], PARTITIONED, "id"
    )

    assert 'src."state" = NEW."state"' in scoped
    assert 'src."state" = NEW."state"' not in global_unique
