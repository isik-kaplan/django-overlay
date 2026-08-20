"""Every index on a base table must exist on its source table too — TODO/16.

The view is a `UNION ALL` over both tables, so a query is only as fast as its
*slower* branch. An index that exists on the tenant's table and not on the
vendor's means half of every filtered query is a sequential scan, and it is
completely silent: the query works, returns the right rows, and is eight times
slower than it should be.

Measured at 3,000,000 rows: `filter(city).count()` went from 47.6ms to 6.2ms
purely from mirroring five indexes onto the source, and the ratio against a
plain table fell from 50x to 6x.

This is the regression test for that. It reuses `introspection.compare_indexes`
— the same function backing `manage.py show_source_indexes` — so it asserts
exactly what that command was written to report, rather than reimplementing the
comparison.
"""

import pytest
from django.apps import apps
from django.db import connection

from django_overlay.introspection import compare_indexes, table_indexes
from django_overlay.sync import resolve_schema


pytestmark = pytest.mark.django_db


def overlay_models_with_a_source():
    return sorted(
        (
            model
            for model in apps.get_models()
            if getattr(model, "_is_overlay_view_model", False) and model.get_source() is not None
        ),
        key=lambda model: model._meta.label,
    )


def ignorable(index) -> bool:
    """Indexes with no counterpart to require on the vendor side: the primary
    key, and soft delete's base-only shadow flag."""
    return index["unique"] or "_overlay_deleted" in index["shape"]


def missing_at_source(cursor, model) -> list[str]:
    source = model.get_source()
    base = table_indexes(cursor, resolve_schema(connection), model._base_model._meta.db_table)
    theirs = table_indexes(cursor, source.schema, source.table)
    _, absent = compare_indexes(theirs, base)
    return [index["shape"] for index in absent if not ignorable(index)]


def test_every_overlay_model_with_a_source_is_index_matched():
    models = overlay_models_with_a_source()
    assert models, "no overlay models with a source were discovered — the sweep would pass vacuously"

    problems = []
    with connection.cursor() as cursor:
        for model in models:
            for shape in missing_at_source(cursor, model):
                problems.append(f"{model._meta.label}: {shape} is on {model._base_model._meta.db_table} only")

    assert not problems, (
        "source tables are missing indexes their base tables have — the view's "
        "vendor branch will sequentially scan:\n  " + "\n  ".join(problems)
    )


def test_the_sweep_would_actually_catch_a_missing_index():
    """A parity test that cannot fail is worse than none. Drop one index and
    confirm the sweep notices, then put it back."""
    from tests.testapp.models import WideCustomer

    with connection.cursor() as cursor:
        assert not missing_at_source(cursor, WideCustomer), "precondition: WideCustomer starts matched"

        cursor.execute("DROP INDEX wcs_city_idx")
        try:
            assert "btree (city)" in missing_at_source(cursor, WideCustomer)
        finally:
            cursor.execute("CREATE INDEX wcs_city_idx ON testapp_shared_widecustomersource (city)")

        assert not missing_at_source(cursor, WideCustomer), "and the index came back"


# ----------------------------------------------- the same thing as a check


def run_check(databases=("default",)):
    from django_overlay.checks import check_source_indexes_match

    return check_source_indexes_match(app_configs=None, databases=databases)


def test_the_system_check_passes_when_everything_matches():
    assert run_check() == []


def test_the_system_check_reports_a_missing_source_index():
    with connection.cursor() as cursor:
        cursor.execute("DROP INDEX wcs_city_idx")
        try:
            warnings = run_check()
        finally:
            cursor.execute("CREATE INDEX wcs_city_idx ON testapp_shared_widecustomersource (city)")

    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.id == "django_overlay.W001"
    assert "(city)" in warning.msg
    assert "testapp_shared_widecustomersource" in warning.msg
    assert "show_source_indexes" in warning.hint


def test_the_system_check_reports_an_index_the_base_table_is_missing():
    """Both directions matter — an index the vendor has and we don't makes our
    branch the slow one."""
    with connection.cursor() as cursor:
        cursor.execute("CREATE INDEX tmp_src_only ON testapp_shared_widecustomersource (email)")
        try:
            warnings = run_check()
        finally:
            cursor.execute("DROP INDEX tmp_src_only")

    assert len(warnings) == 1
    assert "but not on widecustomer" in warnings[0].msg
    assert "(email)" in warnings[0].msg


def test_the_system_check_does_nothing_without_a_database():
    """Registered under Tags.database, so Django passes no databases unless
    asked. It must be inert then rather than reaching for a connection."""
    assert run_check(databases=None) == []
    assert run_check(databases=()) == []


def test_the_system_check_skips_a_source_table_that_does_not_exist_yet():
    """Before migrations there is nothing to compare, and complaining would
    just be noise on a fresh database."""
    from django_overlay.checks import _table_exists

    with connection.cursor() as cursor:
        assert _table_exists(cursor, "public", "testapp_shared_widecustomersource")
        assert not _table_exists(cursor, "public", "no_such_vendor_table")


def test_the_primary_key_and_tombstone_flag_are_not_reported():
    """The base table has a pk index and (with soft delete) may be indexed on
    _overlay_deleted; neither has a counterpart to require."""
    from django_overlay.checks import _ignorable, _index_columns

    assert _index_columns("btree (company_id, created_at)") == ["company_id", "created_at"]
    assert _index_columns("btree (ssn) WHERE (NOT _overlay_deleted)") == ["ssn"]
    assert _index_columns("btree (first_name, last_name) WHERE (NOT _overlay_deleted)") == [
        "first_name",
        "last_name",
    ]
    assert _index_columns("btree ((- id))") == ["(- id)"]
    assert _index_columns("btree (unbalanced") == []
    assert _index_columns("gist (geom)") == ["geom"]
    assert _index_columns("something odd") == []

    assert _ignorable({"unique": True, "shape": "btree (id)"}, "id")
    assert _ignorable({"unique": False, "shape": "btree (_overlay_deleted)"}, "id")
    assert not _ignorable({"unique": False, "shape": "btree (city)"}, "id")
    # *Filtered by* the tombstone flag is not the same as *indexed on* it —
    # this is a uniqueness index on ssn and must still be compared.
    assert not _ignorable({"unique": True, "shape": "btree (ssn) WHERE NOT _overlay_deleted"}, "id")
    # A *unique* index that isn't the pk still wants a counterpart: the
    # uniqueness trigger looks the value up in the source on every insert.
    assert not _ignorable({"unique": True, "shape": "btree (ssn)"}, "id")


# --------------------------------------- W002: relations and uniqueness


def run_relations_check(databases=("default",)):
    from django_overlay.checks import check_source_indexes_cover_relations

    return check_source_indexes_cover_relations(app_configs=None, databases=databases)


def test_relation_check_passes_when_every_source_is_covered():
    assert run_relations_check() == []


def test_relation_check_does_nothing_without_a_database():
    assert run_relations_check(databases=None) == []


def test_relation_check_reports_an_unindexed_foreign_key_on_the_source():
    """Django indexes FK columns on the base table automatically; nothing does
    it on the vendor's, so this is the easy one to get wrong."""
    with connection.cursor() as cursor:
        cursor.execute("DROP INDEX rms_roster_id_idx")
        try:
            warnings = run_relations_check()
        finally:
            cursor.execute("CREATE INDEX rms_roster_id_idx ON testapp_shared_rostermembershipsource (roster_id)")

    assert [w.id for w in warnings] == ["django_overlay.W002"]
    assert "roster_id" in warnings[0].msg
    assert "foreign key" in warnings[0].msg


def test_relation_check_reports_an_unindexed_uniqueness_column():
    """The constraint trigger runs `SELECT 1 FROM source WHERE ssn = NEW.ssn`
    on every insert. Unindexed, that is a sequential scan of the vendor table
    per row — and the parity check cannot see it, because the base side's index
    is a partial unique one that matches nothing on the source."""
    with connection.cursor() as cursor:
        cursor.execute("DROP INDEX uts_ssn_idx")
        try:
            warnings = run_relations_check()
        finally:
            cursor.execute("CREATE INDEX uts_ssn_idx ON testapp_shared_uniquetestsource (ssn)")

    assert [w.id for w in warnings] == ["django_overlay.W002"]
    assert "ssn" in warnings[0].msg
    assert "duplicate on every insert" in warnings[0].msg


def test_a_composite_index_covers_its_leading_column():
    """An index on (a, b) serves lookups on a, so it must not be reported."""
    from django_overlay.checks import _index_columns

    assert _index_columns("btree (roster_id, member_id)")[0] == "roster_id"


def test_comparable_tables_skips_when_a_table_is_missing():
    """Before migrations there is nothing to compare, on either side.

    Stand-ins rather than real models: an OverlayModel cannot be subclassed
    (the metaclass refuses multi-table inheritance), and `_comparable_tables`
    only ever touches these two attributes."""
    from types import SimpleNamespace

    from django_overlay.checks import _comparable_tables
    from django_overlay.sources import SourceTable
    from tests.testapp.models import WideCustomer

    no_source = SimpleNamespace(
        get_source=lambda: SourceTable(schema="public", table="no_such_vendor_table"),
        _base_model=WideCustomer._base_model,
    )
    no_base = SimpleNamespace(
        get_source=WideCustomer.get_source,
        _base_model=SimpleNamespace(_meta=SimpleNamespace(db_table="no_such_base_table")),
    )

    with connection.cursor() as cursor:
        assert _comparable_tables(cursor, connection, no_source) is None
        assert _comparable_tables(cursor, connection, no_base) is None
        assert _comparable_tables(cursor, connection, WideCustomer) is not None


def test_an_index_shape_with_no_columns_is_skipped():
    """Defensive: pg_get_indexdef always yields a parenthesised column list,
    but the comparison must not invent an empty tuple if it ever doesn't."""
    from django_overlay.checks import _covered_column_sets

    assert _covered_column_sets([{"unique": False, "shape": "brin"}], "id") == set()
    assert _covered_column_sets([{"unique": False, "shape": "btree (city)"}], "id") == {("city",)}


def test_only_overlay_unique_constraints_demand_a_source_index():
    """A CheckConstraint has no source-side lookup behind it, so it must not
    ask for an index."""
    from types import SimpleNamespace

    from django.db.models import CheckConstraint, Q

    from django_overlay.checks import _columns_needing_a_source_index

    stub = SimpleNamespace(
        _meta=SimpleNamespace(concrete_fields=[], pk=SimpleNamespace(column="id")),
        _base_model=SimpleNamespace(
            _meta=SimpleNamespace(constraints=[CheckConstraint(condition=Q(age__gte=0), name="age_positive")])
        ),
    )
    assert _columns_needing_a_source_index(stub) == {}


def test_a_model_whose_tables_are_missing_is_skipped_by_the_check(monkeypatch):
    """Covers the loop's skip, which the per-model helper cannot reach."""
    from types import SimpleNamespace

    from django_overlay import checks as overlay_checks
    from django_overlay.sources import SourceTable
    from tests.testapp.models import WideCustomer

    absent = SimpleNamespace(
        get_source=lambda: SourceTable(schema="public", table="no_such_vendor_table"),
        _base_model=WideCustomer._base_model,
    )
    monkeypatch.setattr(overlay_checks, "_overlay_models_with_a_source", lambda: [absent])

    assert overlay_checks.check_source_indexes_match(app_configs=None, databases=("default",)) == []
    assert overlay_checks.check_source_indexes_cover_relations(app_configs=None, databases=("default",)) == []
