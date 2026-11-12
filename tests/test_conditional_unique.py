"""OverlayUniqueConstraint(condition=...): at most one open residence per
household, held across the base table and the source table."""

import datetime
import itertools

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.db.models import Q
from django.test.utils import isolate_apps

from django_overlay import uniqueness
from django_overlay.checks import check_unique_conditions, unique_condition_errors
from django_overlay.constraints import OverlayUniqueConstraint
from django_overlay.models import OverlayMeta, OverlayModel
from django_overlay.sources import SourceTable
from django_overlay.sync import resync_view
from tests.testapp.models import Tenancy, Vendor
from tests.testapp_shared.models import TenancySource


pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 1, 1)


def committed(db_cursor, action):
    with transaction.atomic():
        result = action()
        db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    return result


def refused(db_cursor, action):
    with pytest.raises(IntegrityError):
        committed(db_cursor, action)


# The base half: a partial unique index.


def test_two_open_residences_of_your_own_are_refused(db_cursor):
    Tenancy.objects.create(household=1, kind="residence")
    refused(db_cursor, lambda: Tenancy.objects.create(household=1, kind="residence"))


def test_rows_outside_the_condition_never_collide(db_cursor):
    Tenancy.objects.create(household=2, kind="residence")
    committed(db_cursor, lambda: Tenancy.objects.create(household=2, kind="residence", ended_at=TODAY))
    committed(db_cursor, lambda: Tenancy.objects.create(household=2, kind="mailing"))
    committed(db_cursor, lambda: Tenancy.objects.create(household=2, kind="mailing"))
    assert Tenancy.objects.filter(household=2).count() == 4


def test_the_index_carries_the_condition_and_the_tombstone_predicate(db_cursor):
    db_cursor.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'tenancy_one_open_residence'")
    (indexdef,) = db_cursor.fetchone()
    assert "WHERE" in indexdef
    assert "ended_at IS NULL" in indexdef
    assert "'residence'" in indexdef
    assert "_overlay_deleted" in indexdef


def test_a_soft_deleted_open_residence_frees_the_slot(db_cursor):
    row = Tenancy.objects.create(household=3, kind="residence")
    row.delete()
    committed(db_cursor, lambda: Tenancy.objects.create(household=3, kind="residence"))


# The source half: the trigger.


def test_an_open_residence_colliding_with_an_open_source_residence_is_refused(db_cursor):
    TenancySource.objects.create(household=10, kind="residence")
    refused(db_cursor, lambda: Tenancy.objects.create(household=10, kind="residence"))


def test_a_closed_source_residence_reserves_nothing(db_cursor):
    TenancySource.objects.create(household=11, kind="residence", ended_at=TODAY)
    committed(db_cursor, lambda: Tenancy.objects.create(household=11, kind="residence"))


def test_a_source_row_of_another_kind_reserves_nothing(db_cursor):
    TenancySource.objects.create(household=12, kind="mailing")
    committed(db_cursor, lambda: Tenancy.objects.create(household=12, kind="residence"))


def test_a_new_row_outside_the_condition_is_never_refused(db_cursor):
    TenancySource.objects.create(household=13, kind="residence")
    committed(db_cursor, lambda: Tenancy.objects.create(household=13, kind="residence", ended_at=TODAY))
    committed(db_cursor, lambda: Tenancy.objects.create(household=13, kind="mailing"))


def test_closing_a_source_residence_lets_a_new_one_open(db_cursor):
    """The case the condition exists for: the vendor's open residence is
    closed by an edit, which overrides it, and the household moves."""
    source = TenancySource.objects.create(household=14, kind="residence")
    committed(db_cursor, lambda: Tenancy.objects.filter(id=-source.id).update(ended_at=TODAY))
    committed(db_cursor, lambda: Tenancy.objects.create(household=14, kind="residence"))
    assert Tenancy.objects.filter(household=14, ended_at__isnull=True).count() == 1


def test_reopening_a_closed_row_is_checked(db_cursor):
    """Only the condition's columns change, not the constrained one."""
    TenancySource.objects.create(household=15, kind="residence")
    row = committed(db_cursor, lambda: Tenancy.objects.create(household=15, kind="residence", ended_at=TODAY))
    refused(db_cursor, lambda: Tenancy.objects.filter(id=row.id).update(ended_at=None))


def test_a_soft_deleted_source_residence_reserves_nothing(db_cursor):
    source = TenancySource.objects.create(household=16, kind="residence")
    Tenancy.objects.get(id=-source.id).delete()
    committed(db_cursor, lambda: Tenancy.objects.create(household=16, kind="residence"))


def test_the_resync_rebuilds_the_same_trigger(db_cursor):
    resync_view(Tenancy)
    TenancySource.objects.create(household=17, kind="residence")
    refused(db_cursor, lambda: Tenancy.objects.create(household=17, kind="residence"))
    TenancySource.objects.create(household=18, kind="residence", ended_at=TODAY)
    committed(db_cursor, lambda: Tenancy.objects.create(household=18, kind="residence"))


# Validation and the migration round trip.


def test_validation_applies_the_condition():
    Tenancy.objects.create(household=20, kind="residence")
    with pytest.raises(ValidationError):
        Tenancy(household=20, kind="residence").validate_constraints()
    Tenancy(household=20, kind="residence", ended_at=TODAY).validate_constraints()


def test_the_condition_survives_a_deconstruct_round_trip_with_soft_delete():
    constraint = OverlayUniqueConstraint(
        fields=["household"], condition=Q(kind="residence"), name="c", soft_delete=True
    )
    _, args, kwargs = constraint.deconstruct()
    assert kwargs["condition"] == Q(kind="residence")
    assert kwargs["soft_delete"] is True
    assert OverlayUniqueConstraint(*args, **kwargs) == constraint


def test_the_condition_survives_a_deconstruct_round_trip_without_soft_delete():
    constraint = OverlayUniqueConstraint(fields=["household"], condition=Q(kind="residence"), name="c")
    _, args, kwargs = constraint.deconstruct()
    assert kwargs == {"fields": ("household",), "condition": Q(kind="residence"), "name": "c"}


def test_un_narrowing_keeps_the_callers_condition():
    constraint = OverlayUniqueConstraint(
        fields=["household"], condition=Q(kind="residence"), name="c", soft_delete=True
    )
    assert constraint.without_soft_delete_narrowing().condition == Q(kind="residence")


def test_a_percent_in_the_condition_is_doubled_for_the_trigger():
    constraint = OverlayUniqueConstraint(fields=["household"], condition=Q(kind__startswith="5%"), name="c")
    sql = uniqueness.condition_sql(constraint, Tenancy._base_model, connection.schema_editor())
    # psycopg formats the trigger SQL once; every percent sign must survive it.
    assert "%" in sql % ()
    assert sql % () == sql.replace("%%", "%")


def test_condition_columns_are_listed_once_in_order():
    constraint = OverlayUniqueConstraint(
        fields=["household"], condition=Q(kind="a") | Q(ended_at__isnull=True, kind="b"), name="c"
    )
    assert uniqueness.condition_columns(constraint, Tenancy) == ["kind", "ended_at"]


def test_no_condition_means_no_condition_sql():
    constraint = OverlayUniqueConstraint(fields=["household"], name="c")
    assert uniqueness.condition_sql(constraint, Tenancy, connection.schema_editor()) is None
    assert uniqueness.condition_columns(constraint, Tenancy) == []


# The check: what the source row has to have.


_probe_names = itertools.count(1)


def _probe(condition):
    overlay_meta = type(
        "OverlayMeta",
        (OverlayMeta,),
        {"table_name": "probe_cond", "get_source": staticmethod(lambda: SourceTable(schema="public", table="x"))},
    )
    meta = type(
        "Meta",
        (),
        {
            "app_label": "testapp",
            "constraints": [OverlayUniqueConstraint(fields=["household"], condition=condition, name="probe_cond")],
        },
    )
    return type(
        f"ProbeCondition{next(_probe_names)}",
        (OverlayModel,),
        {
            "__module__": "tests.testapp.models",
            "household": models.IntegerField(),
            "kind": models.CharField(max_length=20),
            "owner": models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="+"),
            "Meta": meta,
            "OverlayMeta": overlay_meta,
        },
    )


def _errors(condition):
    with isolate_apps("tests.testapp"):
        return unique_condition_errors(_probe(condition))


def test_the_check_accepts_the_models_own_fields():
    assert _errors(Q(kind="residence") | Q(household__gt=3) | Q(owner=1)) == []


def test_the_check_refuses_a_lookup_across_a_relation():
    (error,) = _errors(Q(owner__name="x"))
    assert error.id == "django_overlay.E007"
    assert "'probe_cond' has a condition the source-side check can't apply" in error.msg
    assert "no lookup across a relation" in error.hint


def test_the_check_refuses_the_tombstone_column():
    (error,) = _errors(Q(_overlay_deleted=False))
    assert error.id == "django_overlay.E007"
    assert "_overlay_deleted" in error.msg


def test_the_check_refuses_the_primary_key():
    (error,) = _errors(Q(id__gt=0))
    assert error.msg.endswith("can't apply: it names the primary key, id.")


def test_the_registered_check_passes_the_test_models():
    assert check_unique_conditions(None) == []
