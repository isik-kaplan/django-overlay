import pytest
from django.db import connection

from django_overlay.operations import DropOverlayView


pytestmark = pytest.mark.django_db


class _FakeHistoricalModel:
    class _meta:
        db_table = "scratch_deletable"


class _FakeApps:
    def get_model(self, app_label, model_name):
        return _FakeHistoricalModel


def test_drop_overlay_view_removes_the_view_but_leaves_the_table(db_cursor):
    db_cursor.execute("CREATE TABLE scratch_deletable (id serial primary key, label text);")
    db_cursor.execute("CREATE VIEW scratch_deletable_view AS SELECT * FROM scratch_deletable;")

    op = DropOverlayView("testapp", "ScratchDeletable")
    with connection.schema_editor() as schema_editor:
        op.code(_FakeApps(), schema_editor)

    db_cursor.execute("SELECT to_regclass('scratch_deletable_view');")
    assert db_cursor.fetchone()[0] is None
    db_cursor.execute("SELECT to_regclass('scratch_deletable');")
    assert db_cursor.fetchone()[0] is not None

    db_cursor.execute("DROP TABLE scratch_deletable;")


def test_dropping_the_view_first_lets_the_base_table_actually_be_dropped(db_cursor):
    db_cursor.execute("CREATE TABLE scratch_deletable2 (id serial primary key);")
    db_cursor.execute("CREATE VIEW scratch_deletable2_view AS SELECT * FROM scratch_deletable2;")

    class _FakeModel:
        class _meta:
            db_table = "scratch_deletable2"

    class _FakeApps2:
        def get_model(self, app_label, model_name):
            return _FakeModel

    op = DropOverlayView("testapp", "ScratchDeletable2")
    with connection.schema_editor() as schema_editor:
        op.code(_FakeApps2(), schema_editor)
        # Mirrors what DeleteModel does right after — this is exactly the
        # statement that used to fail with "cannot drop table ... because
        # other objects depend on it" before the view was dropped first.
        schema_editor.execute("DROP TABLE scratch_deletable2;")

    db_cursor.execute("SELECT to_regclass('scratch_deletable2');")
    assert db_cursor.fetchone()[0] is None


def test_drop_overlay_view_backward_is_a_no_op():
    # There's no live model left to re-derive the view from once it's
    # deleted — see the docstring. Reversing this op just does nothing.
    op = DropOverlayView("testapp", "ScratchDeletable")
    op.reverse_code(_FakeApps(), schema_editor=None)


def test_drop_overlay_view_is_a_no_op_for_a_model_that_never_had_a_view(db_cursor):
    db_cursor.execute("CREATE TABLE scratch_plain (id serial primary key);")

    class _FakeModel:
        class _meta:
            db_table = "scratch_plain"

    class _FakeApps3:
        def get_model(self, app_label, model_name):
            return _FakeModel

    op = DropOverlayView("testapp", "ScratchPlain")
    with connection.schema_editor() as schema_editor:
        op.code(_FakeApps3(), schema_editor)  # no error, even though scratch_plain_view never existed
        schema_editor.execute("DROP TABLE scratch_plain;")
