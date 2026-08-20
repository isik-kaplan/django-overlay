"""Is soft_delete masking transparent to the ORM?

Every probe asks the same thing: after `.delete()`, does the row behave the
way a deleted row from a plain table behaves? Run with:

    POSTGRES_USER=postgres uv run pytest tests/probe_soft_delete_compat.py \
        -s -q -p no:cacheprovider --no-cov
"""

import pytest
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, connection, models, transaction

from tests.testapp.models import SoftDeleteTest, SoftDeleteTestNote, SoftDeleteUniqueTest
from tests.testapp_shared.models import SoftDeleteTestSource


pytestmark = pytest.mark.django_db

RESULTS = []


class Diverges(Exception):
    pass


def run(name, fn):
    try:
        with transaction.atomic():
            note = fn()
        RESULTS.append((name, "OK", note or ""))
    except Diverges as exc:
        RESULTS.append((name, "DIVERGES", str(exc).strip()[:180]))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, "ERROR", f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:150]}"))


def organic(**kwargs):
    kwargs.setdefault("first_name", "Organic")
    return SoftDeleteTest.objects.create(**kwargs)


def source_row(**kwargs):
    kwargs.setdefault("first_name", "Source")
    src = SoftDeleteTestSource.objects.create(**kwargs)
    return -src.id


def test_soft_delete_orm_compatibility():
    def _gone_from_filter():
        obj = organic()
        pk = obj.pk
        obj.delete()
        if SoftDeleteTest.objects.filter(pk=pk).exists():
            raise Diverges("still visible via filter()")

    run("hidden from filter()/exists()", _gone_from_filter)

    def _source_row_gone():
        pk = source_row()
        SoftDeleteTest.objects.filter(pk=pk).delete()
        if SoftDeleteTest.objects.filter(pk=pk).exists():
            raise Diverges("an untouched source row survived delete()")

    run("hidden after deleting an untouched source row", _source_row_gone)

    def _get_raises():
        obj = organic()
        pk = obj.pk
        obj.delete()
        try:
            SoftDeleteTest.objects.get(pk=pk)
        except ObjectDoesNotExist:
            return
        raise Diverges("get() still returned the row")

    run("get() raises DoesNotExist", _get_raises)

    def _delete_count():
        organic(first_name="C1")
        organic(first_name="C2")
        deleted, per_model = SoftDeleteTest.objects.filter(first_name__startswith="C").delete()
        if deleted != 2:
            raise Diverges(f"delete() reported {deleted}, expected 2 ({per_model})")

    run("delete() returns the right row count", _delete_count)

    def _count_and_aggregate():
        organic(first_name="agg")
        obj = organic(first_name="agg")
        obj.delete()
        n = SoftDeleteTest.objects.filter(first_name="agg").count()
        agg = SoftDeleteTest.objects.filter(first_name="agg").aggregate(n=models.Count("id"))
        if n != 1 or agg["n"] != 1:
            raise Diverges(f"count()={n}, aggregate={agg['n']}, expected 1")

    run("count() / aggregate() exclude it", _count_and_aggregate)

    def _values_and_iterator():
        obj = organic(first_name="vals")
        obj.delete()
        via_values = list(SoftDeleteTest.objects.filter(first_name="vals").values_list("id", flat=True))
        via_iterator = list(SoftDeleteTest.objects.filter(first_name="vals").iterator(chunk_size=1))
        if via_values or via_iterator:
            raise Diverges("still surfaced by values_list()/iterator()")

    run("values_list() / iterator() exclude it", _values_and_iterator)

    def _reverse_join():
        obj = organic(first_name="joined")
        SoftDeleteTestNote.objects.create(target=obj, text="note")
        obj.delete()
        if SoftDeleteTest.objects.filter(notes__text="note").exists():
            raise Diverges("still reachable through a reverse join")

    run("reverse-relation joins exclude it", _reverse_join)

    def _forward_join():
        obj = organic(first_name="fwd")
        note = SoftDeleteTestNote.objects.create(target=obj, text="note")
        obj.delete()
        # The note is gone too (CASCADE), which is the plain-table behaviour.
        if SoftDeleteTestNote.objects.filter(pk=note.pk).exists():
            raise Diverges("the cascading dependent survived")

    run("on_delete=CASCADE removes dependents", _forward_join)

    def _select_related_after_delete():
        obj = organic(first_name="sel")
        note = SoftDeleteTestNote.objects.create(target=obj, text="note")
        SoftDeleteTest.objects.filter(pk=obj.pk).delete()  # no collector, note survives
        got = SoftDeleteTestNote.objects.select_related("target").filter(pk=note.pk).first()
        if got is not None and got.target_id is not None:
            try:
                _ = got.target
            except ObjectDoesNotExist:
                return "dangling note left by a queryset delete resolves to DoesNotExist"
            raise Diverges("select_related() still resolved the masked target")

    run("select_related() past a masked row", _select_related_after_delete)

    def _reset_to_source():
        pk = source_row(first_name="Restorable")
        SoftDeleteTest.objects.filter(pk=pk).delete()
        SoftDeleteTest(pk=pk).reset_to_source()
        if not SoftDeleteTest.objects.filter(pk=pk).exists():
            raise Diverges("reset_to_source() did not bring the source row back")
        return "row comes back with its pristine source values"

    run("reset_to_source() undoes the mask", _reset_to_source)

    def _reinsert_same_pk():
        obj = organic(first_name="reuse")
        pk = obj.pk
        obj.delete()
        try:
            SoftDeleteTest.objects.create(id=pk, first_name="reused")
        except IntegrityError:
            raise Diverges("the tombstone still occupies the pk — a plain table frees it on delete") from None
        return "same pk reusable after delete, as on a plain table"

    run("re-inserting the same pk after delete", _reinsert_same_pk)

    def _tombstone_visible_in_base_table():
        obj = organic(first_name="tomb")
        pk = obj.pk
        obj.delete()
        with connection.cursor() as cursor:
            cursor.execute("SELECT _overlay_deleted FROM softdeletetest WHERE id = %s", [pk])
            row = cursor.fetchone()
        if row is None:
            raise Diverges("no tombstone row — the mask can't survive a source refresh")
        return "tombstone kept in the base table (invisible through the view)"

    run("tombstone is invisible through the view only", _tombstone_visible_in_base_table)

    def _unique_value_reuse():
        obj = SoftDeleteUniqueTest.objects.create(ssn="probe-1", email="probe-1@x", first_name="probe", last_name="one")
        obj.delete()
        try:
            SoftDeleteUniqueTest.objects.create(ssn="probe-1", email="probe-1@x", first_name="probe", last_name="one")
        except IntegrityError:
            raise Diverges(
                "the tombstone still occupies the unique value — a plain table would free it on delete"
            ) from None
        return "value reusable (partial index excludes the tombstone)"

    run("reusing a unique value after delete", _unique_value_reuse)

    def _full_clean_vs_db():
        obj = SoftDeleteUniqueTest.objects.create(ssn="probe-2", email="probe-2@x", first_name="probe", last_name="two")
        obj.delete()
        candidate = SoftDeleteUniqueTest(ssn="probe-2", email="probe-2@x", first_name="probe", last_name="two")
        try:
            candidate.full_clean()
        except ValidationError:
            raise Diverges("full_clean() rejected a value the database would now accept") from None
        try:
            candidate.save()
        except IntegrityError:
            raise Diverges("full_clean() passed but the insert hit the tombstone's unique index") from None
        return "validation and the database agree"

    run("full_clean() agrees with the database about a tombstone", _full_clean_vs_db)

    width = max(len(n) for n, _, _ in RESULTS)
    print("\n\n=== soft_delete ORM compatibility ===")
    for name, status, detail in RESULTS:
        print(f"{status:9} {name:<{width}}  {detail}")
    ok = sum(1 for _, s, _ in RESULTS if s == "OK")
    print(f"\n{ok} OK / {len(RESULTS) - ok} not OK")
