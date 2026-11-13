"""An update that changes nothing, under each RedundantUpdatePolicy.

Every test rebuilds UniqueTest's triggers for the policy it sets, inside the
test's transaction, so the rebuild is rolled back with it.
"""

import warnings

import pytest
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, ProgrammingError, connection, transaction
from django.db.models import F, Value
from django.db.models.expressions import RawSQL
from django.db.models.functions import Concat
from django.test import override_settings

from django_overlay.checks import check_redundant_update_ignore
from django_overlay.exceptions import OverlayConfigurationError, OverlayRedundantUpdate
from django_overlay.models import OverlayMeta, OverlayModel
from django_overlay.redundant_updates import RedundantUpdatePolicy, redundant_update_policy
from django_overlay.sources import SourceTable
from django_overlay.sql import build_instead_of_update_sql
from django_overlay.sync import resync_view
from tests.testapp.models import UniqueTest
from tests.testapp_shared.models import UniqueTestSource


pytestmark = pytest.mark.django_db

Base = UniqueTest._base_model


@pytest.fixture
def policy(monkeypatch):
    """policy(mode, scope=..., ignore=...) sets UniqueTest's OverlayMeta and rebuilds its triggers."""

    def apply(mode, scope=None, ignore=None):
        monkeypatch.setattr(UniqueTest._overlay_meta, "redundant_updates", mode)
        monkeypatch.setattr(UniqueTest._overlay_meta, "redundant_update_scope", scope)
        monkeypatch.setattr(UniqueTest._overlay_meta, "redundant_update_ignore", ignore)
        resync_view(UniqueTest)

    return apply


def source_row(ssn="100-00-0001", notes="vendor"):
    return -UniqueTestSource.objects.create(ssn=ssn, notes=notes).id


def in_base(pk):
    return Base.objects.filter(pk=pk).exists()


def version(pk):
    """The row version's location: any write makes a new one, even inside the
    test's own transaction, where xmin would not move."""
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT ctid::text FROM "{Base._meta.db_table}" WHERE id = %s', [pk])
        return cursor.fetchone()[0]


# The default changes nothing.


def test_by_default_a_redundant_update_still_copies_the_row_in():
    pk = source_row()

    assert UniqueTest.objects.filter(pk=pk).update(ssn="100-00-0001") == 1
    assert in_base(pk)


def test_the_default_policy_is_materialize_everywhere():
    assert redundant_update_policy(UniqueTest) == RedundantUpdatePolicy("materialize", "source_only", ())


# skip, through the trigger.


def test_skip_leaves_a_source_row_where_it_is_and_reports_it_updated(policy):
    policy("skip")
    pk = source_row()

    assert UniqueTest.objects.filter(pk=pk).update(ssn="100-00-0001", notes="vendor") == 1
    assert not in_base(pk)


def test_skip_still_writes_a_real_change(policy):
    policy("skip")
    pk = source_row()

    UniqueTest.objects.filter(pk=pk).update(notes="ours")

    assert in_base(pk)
    assert UniqueTest.objects.get(pk=pk).notes == "ours"


def test_a_skipped_save_neither_inserts_nor_fails(policy):
    """Django answers an UPDATE that touched no row by INSERTing, or with
    update_fields by raising. The trigger reports the skipped row as updated."""
    policy("skip")
    pk = source_row()
    person = UniqueTest.objects.get(pk=pk)

    person.save()
    person.save(update_fields=["notes"])

    assert not in_base(pk)
    assert UniqueTest.objects.filter(ssn="100-00-0001").count() == 1


def test_an_ignored_column_does_not_count_as_a_change(policy):
    policy("skip", ignore=["notes"])
    pk = source_row()

    assert UniqueTest.objects.filter(pk=pk).update(notes="touched") == 1

    assert not in_base(pk)
    assert UniqueTest.objects.get(pk=pk).notes == "vendor"


def test_an_ignored_column_is_still_written_with_a_real_change(policy):
    policy("skip", ignore=["notes"])
    pk = source_row()

    UniqueTest.objects.filter(pk=pk).update(ssn="100-00-0002", notes="touched")

    row = UniqueTest.objects.get(pk=pk)
    assert (row.ssn, row.notes) == ("100-00-0002", "touched")


def test_bulk_update_goes_row_by_row_through_the_guard(policy):
    policy("skip")
    first, second = source_row("100-00-0001"), source_row("100-00-0002")
    rows = list(UniqueTest.objects.filter(pk__in=[first, second]).order_by("pk"))
    rows[0].notes = "ours"

    UniqueTest.objects.bulk_update(rows, ["notes"])

    assert in_base(rows[0].pk)
    assert not in_base(rows[1].pk)


# Scope.


def test_source_only_still_writes_a_redundant_update_to_a_row_already_ours(policy):
    policy("skip", scope="source_only")
    row = UniqueTest.objects.create(ssn="100-00-0003")
    before = version(row.pk)

    UniqueTest.objects.filter(pk=row.pk).update(ssn="100-00-0003")

    assert version(row.pk) != before


def test_scope_all_skips_a_redundant_update_to_a_row_already_ours(policy):
    policy("skip", scope="all")
    row = UniqueTest.objects.create(ssn="100-00-0003")
    before = version(row.pk)

    assert UniqueTest.objects.filter(pk=row.pk).update(ssn="100-00-0003") == 1

    assert version(row.pk) == before


def test_scope_all_still_writes_a_real_change_to_a_row_already_ours(policy):
    policy("skip", scope="all")
    row = UniqueTest.objects.create(ssn="100-00-0003")

    UniqueTest.objects.filter(pk=row.pk).update(notes="changed")

    assert UniqueTest.objects.get(pk=row.pk).notes == "changed"


# warn and raise, through the trigger.


def test_warn_skips(policy):
    policy("warn")
    pk = source_row()

    assert UniqueTest.objects.filter(pk=pk).update(ssn="100-00-0001") == 1
    assert not in_base(pk)


def test_warn_says_so_in_the_database(policy):
    policy("warn")
    pk = source_row()
    notices = []

    def heard(diagnostic):  # read now: a Diagnostic is only valid during the callback
        notices.append((diagnostic.severity, diagnostic.message_primary))

    connection.ensure_connection()
    connection.connection.add_notice_handler(heard)
    try:
        UniqueTest.objects.filter(pk=pk).update(ssn="100-00-0001")
    finally:
        connection.connection.remove_notice_handler(heard)

    assert notices == [
        ("WARNING", f"overlay redundant update: an update of uniquetest_view row {pk} changes nothing, skipped")
    ]


def test_raise_refuses_through_the_trigger(policy):
    policy("raise")
    pk = source_row()

    with pytest.raises(OverlayRedundantUpdate) as refused:
        with transaction.atomic():
            UniqueTest.objects.filter(pk=pk).update(ssn="100-00-0001")

    # The prefix is how an except clause tells it from any other trigger's error.
    assert str(refused.value).startswith(
        f"overlay redundant update: an update of uniquetest_view row {pk} changes nothing\n"
    )
    assert refused.value.__cause__.__cause__.sqlstate == "OV001"

    assert not in_base(pk)


def test_raise_lets_a_real_change_through(policy):
    policy("raise")
    pk = source_row()

    UniqueTest.objects.filter(pk=pk).update(notes="ours")

    assert in_base(pk)


# The expression path, which goes around the view.


def test_an_expression_update_skips_the_rows_it_does_not_change(policy):
    policy("skip")
    empty, filled = source_row("100-00-0001", ""), source_row("100-00-0002", "x")

    # Appending "" changes nothing anywhere; the count is every matched row.
    assert UniqueTest.objects.filter(pk__in=[empty, filled]).update(notes=Concat(F("notes"), Value(""))) == 2

    assert not in_base(empty)
    assert not in_base(filled)


def test_an_expression_update_writes_the_rows_it_changes(policy):
    policy("skip")
    pk = source_row()

    assert UniqueTest.objects.filter(pk=pk).update(notes=Concat(F("notes"), Value("!"))) == 1

    assert in_base(pk)
    assert UniqueTest.objects.get(pk=pk).notes == "vendor!"


def test_an_expression_update_of_only_ignored_columns_changes_nothing(policy):
    policy("skip", ignore=["notes"])
    pk = source_row()

    assert UniqueTest.objects.filter(pk=pk).update(notes=Concat(F("notes"), Value("!"))) == 1

    assert not in_base(pk)


def test_an_expression_update_under_source_only_writes_rows_already_ours(policy):
    policy("skip")
    row = UniqueTest.objects.create(ssn="100-00-0003", notes="n")
    before = version(row.pk)

    assert UniqueTest.objects.filter(pk=row.pk).update(notes=F("notes")) == 1

    assert version(row.pk) != before


def test_an_expression_update_under_scope_all_skips_rows_already_ours(policy):
    policy("skip", scope="all")
    row = UniqueTest.objects.create(ssn="100-00-0003", notes="n")
    before = version(row.pk)

    assert UniqueTest.objects.filter(pk=row.pk).update(notes=F("notes")) == 1

    assert version(row.pk) == before


def test_an_expression_update_warns(policy):
    policy("warn")
    pk = source_row()

    with pytest.warns(UserWarning, match=r"an update of 1 UniqueTest row\(s\) changes nothing, skipped"):
        UniqueTest.objects.filter(pk=pk).update(notes=F("notes"))

    assert not in_base(pk)


def test_an_expression_update_that_changes_everything_does_not_warn(policy):
    policy("warn")
    pk = source_row()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        UniqueTest.objects.filter(pk=pk).update(notes=Concat(F("notes"), Value("!")))


def test_an_expression_update_raises(policy):
    policy("raise")
    pk = source_row()

    with pytest.raises(OverlayRedundantUpdate, match=r"an update of 1 UniqueTest row\(s\) changes nothing"):
        UniqueTest.objects.filter(pk=pk).update(notes=F("notes"))

    assert not in_base(pk)


def test_the_expression_refusal_is_a_programming_error_like_the_triggers():
    assert issubclass(OverlayRedundantUpdate, ProgrammingError)


def test_a_skipped_expression_save_neither_inserts_nor_fails(policy):
    policy("skip")
    pk = source_row()
    person = UniqueTest.objects.get(pk=pk)
    person.notes = F("notes")

    person.save()

    assert not in_base(pk)
    assert UniqueTest.objects.filter(ssn="100-00-0001").count() == 1


def test_a_model_instance_is_compared_by_its_key():
    from django_overlay.models.queryset import _as_expression

    row = UniqueTest(pk=7)
    value = _as_expression(row, UniqueTest._meta.pk)

    assert value.value == 7


# Configuration.


def test_settings_set_the_project_default():
    with override_settings(
        DJANGO_OVERLAY_REDUNDANT_UPDATES="skip",
        DJANGO_OVERLAY_REDUNDANT_UPDATE_SCOPE="all",
        DJANGO_OVERLAY_REDUNDANT_UPDATE_IGNORE=["notes", "not_on_this_model"],
    ):
        assert redundant_update_policy(UniqueTest) == RedundantUpdatePolicy("skip", "all", ("notes",))


def test_the_models_own_setting_wins(monkeypatch):
    monkeypatch.setattr(UniqueTest._overlay_meta, "redundant_updates", "raise")
    monkeypatch.setattr(UniqueTest._overlay_meta, "redundant_update_scope", "source_only")
    monkeypatch.setattr(UniqueTest._overlay_meta, "redundant_update_ignore", ())
    with override_settings(
        DJANGO_OVERLAY_REDUNDANT_UPDATES="skip",
        DJANGO_OVERLAY_REDUNDANT_UPDATE_SCOPE="all",
        DJANGO_OVERLAY_REDUNDANT_UPDATE_IGNORE=["notes"],
    ):
        assert redundant_update_policy(UniqueTest) == RedundantUpdatePolicy("raise", "source_only", ())


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("DJANGO_OVERLAY_REDUNDANT_UPDATES", "never", "settings.DJANGO_OVERLAY_REDUNDANT_UPDATES must be one of"),
        (
            "DJANGO_OVERLAY_REDUNDANT_UPDATE_SCOPE",
            "some",
            "settings.DJANGO_OVERLAY_REDUNDANT_UPDATE_SCOPE must be one of",
        ),
        ("DJANGO_OVERLAY_REDUNDANT_UPDATE_IGNORE", "notes", "must be a list or tuple of field names"),
        ("DJANGO_OVERLAY_REDUNDANT_UPDATE_IGNORE", [1], "must be a list or tuple of field names"),
    ],
)
def test_a_bad_setting_is_refused(setting, value, message):
    with override_settings(**{setting: value}), pytest.raises(ImproperlyConfigured, match=message):
        redundant_update_policy(UniqueTest)


def _declare(**overlay_meta):
    from django.test.utils import isolate_apps

    with isolate_apps("tests.testapp"):
        meta = type(
            "OverlayMeta",
            (OverlayMeta,),
            {"get_source": staticmethod(lambda: SourceTable(schema="public", table="x")), **overlay_meta},
        )
        return type(
            "ProbeRedundant",
            (OverlayModel,),
            {
                "__module__": "tests.testapp.models",
                "notes": __import__("django.db.models", fromlist=["CharField"]).CharField(max_length=5),
                "Meta": type("Meta", (), {"app_label": "testapp"}),
                "OverlayMeta": meta,
            },
        )


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("redundant_updates", "never", "ProbeRedundant.OverlayMeta.redundant_updates must be None or one of"),
        ("redundant_update_scope", "some", "ProbeRedundant.OverlayMeta.redundant_update_scope must be None or one of"),
        ("redundant_update_ignore", "notes", "must be None or a list or tuple of field names"),
        ("redundant_update_ignore", [1], "must be None or a list or tuple of field names"),
    ],
)
def test_a_bad_model_option_is_refused_when_declared(option, value, message):
    with pytest.raises(OverlayConfigurationError, match=message):
        _declare(**{option: value})


def test_the_check_refuses_an_ignored_name_the_model_lacks(monkeypatch):
    monkeypatch.setattr(UniqueTest._overlay_meta, "redundant_update_ignore", ["notes", "updated_at", "revision"])

    (error,) = check_redundant_update_ignore(None)

    assert error.id == "django_overlay.E008"
    assert error.msg == (
        "UniqueTest.OverlayMeta.redundant_update_ignore names 'updated_at', 'revision', which UniqueTest has no "
        "field for."
    )
    assert isinstance(error, checks.Error)


def test_the_check_passes_the_test_models():
    assert check_redundant_update_ignore(None) == []


def test_a_trigger_built_with_no_policy_has_no_guard():
    sql = build_instead_of_update_sql("v", "public", "b", ["id", "x"])

    assert "redundant" not in sql


def test_a_guard_with_every_column_ignored_treats_every_update_as_redundant():
    sql = build_instead_of_update_sql(
        "v", "public", "b", ["id", "x"], redundant=RedundantUpdatePolicy("skip", "source_only", ("id", "x"))
    )

    assert "IF NOT (FALSE) THEN" in sql


def test_a_policy_says_what_it_is():
    assert repr(RedundantUpdatePolicy("skip", "all", ("updated_at",))) == (
        "RedundantUpdatePolicy('skip', 'all', ('updated_at',))"
    )


def test_save_and_bulk_update_raise_the_same_class(policy):
    policy("raise")
    pk = source_row()
    row = UniqueTest.objects.get(pk=pk)

    with pytest.raises(OverlayRedundantUpdate), transaction.atomic():
        row.save()
    with pytest.raises(OverlayRedundantUpdate), transaction.atomic():
        UniqueTest.objects.bulk_update([row], ["ssn"])


def test_raw_sql_through_the_view_gets_the_triggers_own_error(policy):
    policy("raise")
    pk = source_row()

    with pytest.raises(DatabaseError) as refused, transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE uniquetest_view SET ssn = ssn WHERE id = %s", [pk])

    assert not isinstance(refused.value, OverlayRedundantUpdate)
    assert refused.value.__cause__.sqlstate == "OV001"
    assert str(refused.value).startswith("overlay redundant update: ")


def test_another_database_error_is_left_alone():
    with pytest.raises(ProgrammingError) as raised:
        with transaction.atomic():
            UniqueTest.objects.filter(pk=1).update(ssn=RawSQL("no_such_column", ()))

    assert not isinstance(raised.value, OverlayRedundantUpdate)


def test_the_expression_refusal_starts_with_the_prefix(policy):
    policy("raise")
    pk = source_row()

    with pytest.raises(OverlayRedundantUpdate) as refused:
        UniqueTest.objects.filter(pk=pk).update(notes=F("notes"))

    assert str(refused.value).startswith("overlay redundant update: ")
