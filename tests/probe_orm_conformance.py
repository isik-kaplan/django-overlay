"""Throwaway probe: which parts of the Django ORM behave identically on an
OverlayModel (a view + INSTEAD OF triggers) vs a plain table?

Not a real test — it never fails. It runs every probe in its own savepoint
and prints an OK / DIVERGES / ERROR table at the end. Run with:

    POSTGRES_USER=postgres uv run pytest tests/test_orm_conformance_probe.py \
        -s -q -p no:cacheprovider --no-cov
"""

import traceback

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, models, transaction
from django.db.models import Count, Exists, F, OuterRef, Q, Subquery, Window
from django.db.models.functions import RowNumber

from tests.testapp.models import Address, AddressNote, Person, PersonProfile, SoftDeleteTest, UniqueTest
from tests.testapp_shared.models import AddressSource, PersonSource, SoftDeleteTestSource, UniqueTestSource


pytestmark = pytest.mark.django_db

RESULTS = []


class Diverges(Exception):
    """Raised by a probe that ran without a DB error but behaved differently
    from a plain table. Rolls its savepoint back on the way out."""


def record(name, status, detail=""):
    RESULTS.append((name, status, detail))


def run(name, fn, flush_deferred=True):
    """Each probe gets its own savepoint so one failure can't poison the rest.

    `flush_deferred` fires the DEFERRABLE INITIALLY DEFERRED constraint
    triggers inside the probe, so a violation is attributed here instead of
    blowing up in test teardown."""
    try:
        with transaction.atomic():
            note = fn()
            if flush_deferred:
                with connection.cursor() as cur:
                    cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
                    cur.execute("SET CONSTRAINTS ALL DEFERRED")
        record(name, "OK", note or "")
    except Diverges as exc:
        record(name, "DIVERGES", str(exc).strip()[:200])
    except Exception as exc:  # noqa: BLE001 - the point of the probe
        record(name, "ERROR", f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:160]}")


def diverges(name, detail):
    record(name, "DIVERGES", detail)


def source_person(**kwargs):
    kwargs.setdefault("first_name", "Src")
    kwargs.setdefault("age", 40)
    src = PersonSource.objects.create(**kwargs)
    return src, -src.id  # NEGATIVE_ID strategy: the view exposes -id


def test_orm_conformance_probe():
    # ---------------------------------------------------------------- writes
    def _create():
        p = Person.objects.create(first_name="A", age=1)
        assert p.pk is not None, "create() did not return a pk"
        assert Person.objects.filter(pk=p.pk).exists()

    run("create() returns a usable pk", _create)

    def _bulk_create():
        objs = Person.objects.bulk_create([Person(first_name=f"B{i}", age=i) for i in range(3)])
        pks = [o.pk for o in objs]
        if any(pk is None for pk in pks):
            return "rows inserted but pks NOT returned on the objects"
        return "pks returned"

    run("bulk_create()", _bulk_create)

    def _bulk_create_ignore():
        from django_overlay.models import OverlayConfigurationError

        try:
            Person.objects.bulk_create([Person(first_name="C", age=1)], ignore_conflicts=True)
        except OverlayConfigurationError:
            return "rejected up front (a view has no unique index to conflict against)"
        raise Diverges("accepted, but the conflict clause lands on the view and does nothing")

    run("bulk_create(ignore_conflicts=True)", _bulk_create_ignore)

    def _bulk_create_update():
        from django_overlay.models import OverlayConfigurationError

        p = Person.objects.create(first_name="D", age=1)
        try:
            Person.objects.bulk_create(
                [Person(id=p.pk, first_name="D2", age=2)],
                update_conflicts=True,
                update_fields=["first_name"],
                unique_fields=["id"],
            )
        except OverlayConfigurationError:
            return "rejected up front (a view has no unique index to conflict against)"
        raise Diverges("accepted, but Postgres has no unique index on the view to match")

    run("bulk_create(update_conflicts=True)", _bulk_create_update)

    def _bulk_update_materialized():
        p = Person.objects.create(first_name="E", age=1)
        p.age = 99
        Person.objects.bulk_update([p], ["age"])
        assert Person.objects.get(pk=p.pk).age == 99

    run("bulk_update() on an organic row", _bulk_update_materialized)

    def _bulk_update_source_only():
        _, vid = source_person(first_name="F")
        p = Person.objects.get(pk=vid)
        p.age = 77
        Person.objects.bulk_update([p], ["age"])
        assert Person.objects.get(pk=vid).age == 77, "bulk_update did not materialize the source row"

    run("bulk_update() on a source-only row", _bulk_update_source_only)

    def _get_or_create():
        obj, created = Person.objects.get_or_create(first_name="G", defaults={"age": 5})
        assert created
        obj2, created2 = Person.objects.get_or_create(first_name="G", defaults={"age": 5})
        assert not created2 and obj2.pk == obj.pk

    run("get_or_create()", _get_or_create)

    def _update_or_create_source_row():
        _, vid = source_person(first_name="H")
        obj, created = Person.objects.update_or_create(pk=vid, defaults={"age": 12})
        assert not created and obj.age == 12

    run("update_or_create() on a source-only row", _update_or_create_source_row)

    def _save_update_fields():
        _, vid = source_person(first_name="I", age=1)
        p = Person.objects.get(pk=vid)
        p.age = 42
        p.save(update_fields=["age"])
        fresh = Person.objects.get(pk=vid)
        assert fresh.age == 42 and fresh.first_name == "I"

    run("save(update_fields=[...]) on a source-only row", _save_update_fields)

    def _save_force_insert_explicit_pk():
        Person.objects.create(id=123456, first_name="J", age=1)
        assert Person.objects.filter(pk=123456).exists()

    run("create() with an explicit pk", _save_force_insert_explicit_pk)

    def _qs_update_f():
        p = Person.objects.create(first_name="K", age=10)
        n = Person.objects.filter(pk=p.pk).update(age=F("age") + 5)
        assert n == 1
        assert Person.objects.get(pk=p.pk).age == 15

    run("queryset.update() with F()", _qs_update_f)

    def _qs_update_rowcount_source():
        _, v1 = source_person(first_name="L1")
        _, v2 = source_person(first_name="L2")
        n = Person.objects.filter(pk__in=[v1, v2]).update(age=1)
        assert n == 2, f"expected rowcount 2, got {n}"

    run("queryset.update() rowcount over source-only rows", _qs_update_rowcount_source)

    def _qs_update_noop_value():
        p = Person.objects.create(first_name="M", age=3)
        n = Person.objects.filter(pk=p.pk).update(age=3)
        return f"rowcount={n} when the new value equals the old"

    run("queryset.update() that changes nothing", _qs_update_noop_value)

    # -------------------------------------------------------------- deletes
    def _delete_organic():
        p = Person.objects.create(first_name="N", age=1)
        p.delete()
        assert not Person.objects.filter(pk=p.pk).exists()

    run("delete() an organic row", _delete_organic)

    def _delete_source_only():
        _, vid = source_person(first_name="O")
        deleted, _ = Person.objects.filter(pk=vid).delete()
        if Person.objects.filter(pk=vid).exists():
            raise Diverges(f"reported {deleted} row(s) deleted, but the row is still visible through the view")

    run("delete() an untouched source row (hard-delete model)", _delete_source_only)

    def _delete_materialized():
        _, vid = source_person(first_name="P", age=1)
        Person.objects.filter(pk=vid).update(age=2)  # materialize
        Person.objects.filter(pk=vid).delete()
        if Person.objects.filter(pk=vid).exists():
            raise Diverges("delete() only dropped the base copy — the row reverted to its pristine source values")

    run("delete() a materialized source row (hard-delete model)", _delete_materialized)

    def _delete_soft():
        src = SoftDeleteTestSource.objects.create(first_name="Q")
        vid = -src.id
        SoftDeleteTest.objects.filter(pk=vid).delete()
        assert not SoftDeleteTest.objects.filter(pk=vid).exists()

    run("delete() a source row on a soft_delete model", _delete_soft)

    def _cascade_delete():
        a = Address.objects.create(street="s", city="c")
        AddressNote.objects.create(address=a, text="t")
        a.delete()
        assert not AddressNote.objects.filter(address_id=a.pk).exists()

    run("on_delete=CASCADE through OverlayForeignKey", _cascade_delete)

    def _insert_then_delete_same_transaction():
        """A real FK is happy here: the child is gone before COMMIT. A
        deferred constraint trigger re-checks the queued INSERT event
        regardless."""
        a = Address.objects.create(street="s3", city="c3")
        note = AddressNote.objects.create(address=a, text="t")
        note.delete()
        a.delete()

    run("insert child + delete it and its parent in one transaction", _insert_then_delete_same_transaction)

    # ------------------------------------------------------------- locking
    # select_for_update() is refused: FOR UPDATE against a view with INSTEAD OF
    # triggers locks nothing, so it would look like mutual exclusion and give
    # none. This probe used to report it OK, which was the false-positive that
    # hid the problem. tests/test_select_for_update.py measures it properly.
    def _select_for_update():
        from django_overlay.exceptions import OverlayConfigurationError

        for build in (
            lambda: Person.objects.select_for_update(),
            lambda: Person.objects.select_for_update(skip_locked=True),
            lambda: Person.objects.select_for_update(of=("self",)),
        ):
            try:
                build()
            except OverlayConfigurationError:
                continue
            raise Diverges("accepted, but Postgres locks no rows through a view")
        return "refused up front (a view's rows can't be locked)"

    run("select_for_update() in all its forms", _select_for_update)

    # --------------------------------------------------------------- reads
    def _basic_filters():
        source_person(first_name="R", age=50)
        Person.objects.create(first_name="R2", age=51)
        assert Person.objects.filter(age__gte=50).count() >= 2
        assert Person.objects.filter(Q(first_name__startswith="R") | Q(age=999)).exists()

    run("filter / Q / count", _basic_filters)

    def _aggregate():
        source_person(first_name="S", age=10)
        Person.objects.create(first_name="S2", age=20)
        agg = Person.objects.aggregate(n=Count("id"), avg=models.Avg("age"))
        assert agg["n"] >= 2

    run("aggregate() across base + source", _aggregate)

    def _values_annotate_group_by():
        source_person(first_name="T", age=1)
        Person.objects.create(first_name="T", age=2)
        rows = list(Person.objects.filter(first_name="T").values("first_name").annotate(n=Count("id")))
        assert rows and rows[0]["n"] == 2, rows

    run("values().annotate() GROUP BY across base + source", _values_annotate_group_by)

    def _distinct_on():
        source_person(first_name="U", age=1)
        Person.objects.create(first_name="U", age=2)
        rows = list(Person.objects.filter(first_name="U").order_by("first_name", "id").distinct("first_name"))
        assert len(rows) == 1

    run("distinct('field') (DISTINCT ON)", _distinct_on)

    def _order_and_slice():
        source_person(first_name="V")
        Person.objects.create(first_name="V2")
        list(Person.objects.order_by("-id")[:5])

    run("order_by + slicing", _order_and_slice)

    def _pk_ordering_semantics():
        _, vid = source_person(first_name="W-source")
        organic = Person.objects.create(first_name="W-organic")
        ids = list(Person.objects.filter(pk__in=[vid, organic.pk]).order_by("id").values_list("id", flat=True))
        if ids[0] != vid:
            raise Diverges(f"unexpected order {ids}")
        return f"source row sorts first ({ids[0]} < {ids[1]}) — NEGATIVE_ID makes order_by('id') != insertion order"

    run("order_by('id') insertion-order semantics", _pk_ordering_semantics)

    def _in_bulk():
        p = Person.objects.create(first_name="X")
        assert Person.objects.in_bulk([p.pk])[p.pk].first_name == "X"

    run("in_bulk()", _in_bulk)

    def _iterator_server_side():
        Person.objects.create(first_name="Y")
        assert len(list(Person.objects.iterator(chunk_size=1))) >= 1

    run("iterator(chunk_size=...) (server-side cursor)", _iterator_server_side)

    def _subquery_exists():
        p = Person.objects.create(first_name="Z")
        PersonProfile.objects.create(person=p, bio="b")
        qs = Person.objects.annotate(has=Exists(PersonProfile.objects.filter(person=OuterRef("pk")))).filter(has=True)
        assert qs.filter(pk=p.pk).exists()
        qs2 = Person.objects.annotate(
            bio=Subquery(PersonProfile.objects.filter(person=OuterRef("pk")).values("bio")[:1])
        )
        assert qs2.get(pk=p.pk).bio == "b"

    run("Exists() / Subquery() / OuterRef()", _subquery_exists)

    def _window():
        Person.objects.create(first_name="AA")
        list(Person.objects.annotate(rn=Window(RowNumber(), order_by=F("id").asc()))[:5])

    run("Window functions", _window)

    def _select_related():
        p = Person.objects.create(first_name="AB")
        prof = PersonProfile.objects.create(person=p, bio="b")
        got = PersonProfile.objects.select_related("person").get(pk=prof.pk)
        assert got.person.first_name == "AB"

    run("select_related() across an OverlayForeignKey", _select_related)

    def _prefetch_related():
        a = Address.objects.create(street="s", city="c")
        AddressNote.objects.create(address=a, text="t")
        got = list(Address.objects.filter(pk=a.pk).prefetch_related("notes"))[0]
        assert len(got.notes.all()) == 1

    run("prefetch_related() reverse of an OverlayForeignKey", _prefetch_related)

    def _reverse_join_filter():
        a = Address.objects.create(street="s2", city="c2")
        AddressNote.objects.create(address=a, text="findme")
        assert Address.objects.filter(notes__text="findme").exists()

    run("filtering across a reverse relation join", _reverse_join_filter)

    def _union():
        Person.objects.create(first_name="AC")
        qs = Person.objects.filter(first_name="AC").union(Person.objects.filter(first_name="nope"))
        assert len(list(qs)) == 1

    run("QuerySet.union()", _union)

    def _raw():
        Person.objects.create(first_name="AD")
        assert len(list(Person.objects.raw("SELECT * FROM person_view WHERE first_name = %s", ["AD"]))) == 1

    run("Model.objects.raw()", _raw)

    def _refresh_from_db():
        p = Person.objects.create(first_name="AE", age=1)
        Person.objects.filter(pk=p.pk).update(age=9)
        p.refresh_from_db()
        assert p.age == 9

    run("refresh_from_db()", _refresh_from_db)

    def _only_defer():
        p = Person.objects.create(first_name="AF", age=1)
        assert Person.objects.only("first_name").get(pk=p.pk).age == 1
        assert Person.objects.defer("age").get(pk=p.pk).first_name == "AF"

    run("only() / defer()", _only_defer)

    def _explain():
        Person.objects.filter(age__gt=0).explain()

    run("QuerySet.explain()", _explain)

    def _latest():
        Person.objects.create(first_name="AG")
        Person.objects.order_by("id").last()
        Person.objects.latest("id")

    run("latest() / last()", _latest)

    # ------------------------------------------------- validation & integrity
    # Constraint *timing* lives in tests/test_constraint_timing.py: measuring
    # it here gave the wrong answer, because run() issues SET CONSTRAINTS ALL
    # DEFERRED between probes and that overrides INITIALLY IMMEDIATE. It also
    # needs a real COMMIT to observe, which this file's transaction never
    # reaches. Short version: the FK trigger matches Django's own FK exactly
    # (both deferred on Postgres) and the unique trigger matches a native
    # unique index (both at the statement).

    def _constraints_reachable_from_the_view_model():
        # _meta.constraints stays empty by design (the DDL belongs to the base
        # model); what matters is that validation can still reach them.
        names = [c.name for model_class, cs in UniqueTest(ssn="x").get_constraints() for c in cs]
        if names:
            return f"via get_constraints(): {names}"
        raise Diverges("get_constraints() is empty — full_clean()/ModelForm can't see them")

    run("Meta.constraints reachable from the queried model", _constraints_reachable_from_the_view_model)

    def _full_clean_catches_source_duplicate():
        UniqueTestSource.objects.create(ssn="dup-3")
        obj = UniqueTest(ssn="dup-3")
        try:
            obj.full_clean()
        except ValidationError:
            return "full_clean() raised ValidationError"
        raise Diverges("full_clean() passed a value the DB trigger will reject at COMMIT")

    run("full_clean() catches a source-side unique collision", _full_clean_catches_source_duplicate)

    def _validate_constraints():
        UniqueTest.objects.create(ssn="dup-4")
        obj = UniqueTest(ssn="dup-4")
        try:
            obj.validate_constraints()
        except ValidationError:
            return "validate_constraints() raised"
        raise Diverges("validate_constraints() is a no-op on the view model (no constraints attached)")

    run("validate_constraints() on the view model", _validate_constraints)

    def _validate_unique_across_view():
        src = AddressSource.objects.create(street="s", city="c")
        assert Address.objects.filter(pk=-src.id).exists()

    run("source rows visible for ORM-side uniqueness lookups", _validate_unique_across_view)

    # ------------------------------------------------------------ tooling
    def _serialization():
        from django.core import serializers

        Person.objects.create(first_name="AH")
        data = serializers.serialize("json", Person.objects.all()[:1])
        assert data

    run("serializers.serialize() (dumpdata)", _serialization)

    def _deserialization_save():
        from django.core import serializers

        p = Person.objects.create(first_name="AI", age=1)
        data = serializers.serialize("json", [p])
        for obj in serializers.deserialize("json", data):
            obj.save()  # loaddata path: force_insert-ish raw save

    run("serializers.deserialize().save() (loaddata)", _deserialization_save)

    def _truncate_visibility():
        names = connection.introspection.table_names()
        if "person_view" in names:
            raise Diverges("the view shows up in introspection.table_names() — flush/TRUNCATE would target it")
        return "views excluded from table_names(), so flush()/TRUNCATE skips them"

    run("flush() / TRUNCATE introspection safety", _truncate_visibility)

    # ------------------------------------------------------------- report
    print("\n\n=== ORM conformance probe ===")
    width = max(len(n) for n, _, _ in RESULTS)
    for name, status, detail in RESULTS:
        print(f"{status:9} {name:<{width}}  {detail}")
    print(f"\n{sum(1 for _, s, _ in RESULTS if s == 'OK')} OK / {sum(1 for _, s, _ in RESULTS if s != 'OK')} not OK")
    print(traceback.format_exc() if False else "")
