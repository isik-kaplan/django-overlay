from django_overlay import sql
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
