import pytest
from django.db import IntegrityError, transaction

from tests.testapp.models import MetaTest, Person


pytestmark = pytest.mark.django_db


def test_view_model_gets_the_declared_ordering_and_verbose_name():
    assert MetaTest._meta.ordering == ["name"]
    assert MetaTest._meta.verbose_name == "meta test"


def test_base_model_does_not_get_the_view_only_options():
    base = MetaTest.base_table()
    assert base._meta.ordering != ["name"]
    assert base._meta.verbose_name != "meta test"


def test_base_model_gets_the_declared_constraint():
    base = MetaTest.base_table()
    names = {c.name for c in base._meta.constraints}
    assert "metatest_name_not_empty" in names


def test_base_model_gets_the_declared_index():
    base = MetaTest.base_table()
    names = {i.name for i in base._meta.indexes}
    assert "metatest_name_idx" in names
    assert base._meta.indexes[0].name not in {i.name for i in MetaTest._meta.indexes}


def test_base_model_gets_the_declared_unique_constraint():
    # unique_together isn't allowed on an overlay model — see
    # django_overlay/uniqueness.py — so this is declared as an
    # OverlayUniqueConstraint and lands with the rest of Meta.constraints.
    assert "metatest_name_unique" in {c.name for c in MetaTest.base_table()._meta.constraints}
    assert MetaTest._meta.constraints == []


def test_base_model_gets_the_declared_table_comment():
    assert MetaTest.base_table()._meta.db_table_comment == "Meta forwarding test fixture"
    assert MetaTest._meta.db_table_comment == ""


def test_the_index_is_actually_created_in_postgres(db_cursor):
    db_cursor.execute("SELECT indexname FROM pg_indexes WHERE indexname = 'metatest_name_idx'")
    assert db_cursor.fetchone() is not None


def test_the_unique_constraint_is_actually_enforced_by_postgres():
    MetaTest.objects.create(name="Duplicate")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            MetaTest.objects.create(name="Duplicate")


def test_the_constraint_is_actually_enforced_by_postgres():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            MetaTest.objects.create(name="")


def test_ordering_is_honored_on_the_view_models_default_queryset():
    MetaTest.objects.create(name="Zoe")
    MetaTest.objects.create(name="Amy")
    assert list(MetaTest.objects.values_list("name", flat=True)) == ["Amy", "Zoe"]


# The base manager is what instance.save() writes through, so it has to be the
# overlay one or save() misses the routing that update() gets.


def test_the_base_manager_is_the_overlay_manager():
    from django_overlay.models import OverlayQuerySet

    assert Person._base_manager is Person.objects
    assert isinstance(Person._base_manager.all(), OverlayQuerySet)


def test_a_declared_manager_keeps_its_own_base_manager():
    """Replacing someone's custom manager would be worse than missing the
    routing, so a model that declares one is left alone."""
    from tests.testapp.models import CustomManagerTest

    assert CustomManagerTest._meta.base_manager_name is None


def test_choosing_the_base_manager_does_not_ask_for_a_migration():
    """base_manager_name is set on _meta rather than declared in Meta. In Meta
    it lands in original_attrs, which the autodetector compares — so every
    project using the library would owe an AlterModelOptions migration for a
    manager it never chose."""
    assert "base_manager_name" not in Person._meta.original_attrs
    assert Person._meta.base_manager_name == "objects"
