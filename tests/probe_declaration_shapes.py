"""Second-pass probe: model-declaration shapes the test app never exercises."""

import pytest
from django.core import checks
from django.db import IntegrityError, models, transaction
from django.test.utils import isolate_apps

from django_overlay.fields import OverlayForeignKey
from django_overlay.models import OverlayMeta, OverlayModel
from django_overlay.sources import SourceTable
from tests.testapp.models import Person
from tests.testapp_shared.models import PersonSource


def _fail_on_errors(model_name):
    """Only the errors about the model this probe just declared — earlier
    probes leave their own broken models in the app registry."""
    errors = [e for e in checks.run_checks() if e.is_serious() and model_name in str(e.obj) + e.msg]
    if errors:
        raise AssertionError("; ".join(f"{e.id} {e.msg}" for e in errors[:2]))


def _report(name, fn):
    try:
        fn()
        print(f"OK       {name}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAILS    {name}\n         {type(exc).__name__}: {str(exc).strip().splitlines()[0][:200]}")


@isolate_apps("tests.testapp")
def test_fk_with_explicit_related_name_on_an_overlay_model():
    """The metaclass deepcopies every concrete field onto BOTH the hidden
    base model and the view model. A relation field with an explicit
    related_name would then be declared twice against the same target."""

    def build():
        class ProbeVendor(models.Model):
            class Meta:
                app_label = "testapp"

        class WithRelatedName(OverlayModel):
            vendor = models.ForeignKey(ProbeVendor, on_delete=models.CASCADE, related_name="widgets")

            class Meta:
                app_label = "testapp"

            class OverlayMeta(OverlayMeta):
                table_name = "probe_withrelatedname"

                @staticmethod
                def get_source():
                    return None

        _fail_on_errors("WithRelatedName")

    _report("plain FK with related_name= declared on an OverlayModel", build)


@isolate_apps("tests.testapp")
def test_fk_without_related_name_on_an_overlay_model():
    def build():
        class ProbeVendor2(models.Model):
            class Meta:
                app_label = "testapp"

        class WithoutRelatedName(OverlayModel):
            vendor = models.ForeignKey(ProbeVendor2, on_delete=models.CASCADE)

            class Meta:
                app_label = "testapp"

            class OverlayMeta(OverlayMeta):
                table_name = "probe_withoutrelatedname"

                @staticmethod
                def get_source():
                    return None

        _fail_on_errors("WithoutRelatedName")

    _report("plain FK with no related_name declared on an OverlayModel", build)


@isolate_apps("tests.testapp")
def test_overlay_fk_from_one_overlay_model_to_another():
    def build():
        class OverlayToOverlay(OverlayModel):
            person = OverlayForeignKey(Person, on_delete=models.CASCADE, related_name="probe_children")

            class Meta:
                app_label = "testapp"

            class OverlayMeta(OverlayMeta):
                table_name = "probe_overlaytooverlay"

                @staticmethod
                def get_source():
                    return SourceTable(schema="public", table="testapp_shared_personsource")

        _fail_on_errors("OverlayToOverlay")

    _report("OverlayForeignKey declared on an OverlayModel (overlay -> overlay)", build)


@isolate_apps("tests.testapp")
def test_self_referential_overlay_fk():
    def build():
        class SelfRef(OverlayModel):
            parent = OverlayForeignKey("self", null=True, on_delete=models.SET_NULL)

            class Meta:
                app_label = "testapp"

            class OverlayMeta(OverlayMeta):
                table_name = "probe_selfref"

                @staticmethod
                def get_source():
                    return None

        _fail_on_errors("SelfRef")

    _report("self-referential OverlayForeignKey", build)


@isolate_apps("tests.testapp")
def test_model_inheritance_from_a_concrete_overlay_model():
    def build():
        class Child(Person):
            extra = models.CharField(max_length=10)

            class Meta:
                app_label = "testapp"

    _report("subclassing a concrete OverlayModel (multi-table inheritance)", build)


@pytest.mark.django_db
def test_bulk_create_ignore_conflicts_with_a_real_conflict():
    """Used to silently not ignore the conflict: the ON CONFLICT clause lands
    on the view (no unique index there, so nothing ever conflicts) while the
    real INSERT happens inside the trigger without one. Now refused up front."""
    from django_overlay.exceptions import OverlayConfigurationError

    p = Person.objects.create(first_name="dup", age=1)
    try:
        Person.objects.bulk_create([Person(id=p.pk, first_name="dup2", age=2)], ignore_conflicts=True)
        print("FAILS    bulk_create(ignore_conflicts=True) silently accepted a real pk conflict")
    except OverlayConfigurationError:
        print("OK       bulk_create(ignore_conflicts=True) refused up front")
    except IntegrityError as exc:
        print(f"FAILS    bulk_create(ignore_conflicts=True) raised IntegrityError\n         {exc}".strip())


@pytest.mark.django_db
def test_update_through_a_related_join():
    src = PersonSource.objects.create(first_name="joinme", age=1)
    n = Person.objects.filter(pk=-src.id, first_name="joinme").update(age=5)
    print(f"OK       queryset.update() through a filter on a source row: rowcount={n}")


@pytest.mark.django_db(transaction=True)
def test_savepoint_rollback_after_a_deferred_violation():
    """try/except IntegrityError around a save is the normal Django idiom.
    With a deferred trigger the error lands on the outermost commit, so the
    except block is in the wrong place entirely."""
    from tests.testapp.models import AddressNote

    caught_where = "nowhere"
    try:
        with transaction.atomic():
            try:
                AddressNote.objects.create(address_id=-424242, text="bad")
            except IntegrityError:
                caught_where = "at the statement"
    except IntegrityError:
        caught_where = "at the outer COMMIT"
    print(f"INFO     OverlayForeignKey violation surfaced: {caught_where}")
