import pytest
from django.db import IntegrityError, transaction

from tests.testapp.models import MetaTest


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


def test_base_model_gets_the_declared_unique_together():
    assert MetaTest.base_table()._meta.unique_together == (("name",),)
    assert MetaTest._meta.unique_together == ()


def test_base_model_gets_the_declared_table_comment():
    assert MetaTest.base_table()._meta.db_table_comment == "Meta forwarding test fixture"
    assert MetaTest._meta.db_table_comment == ""


def test_the_index_is_actually_created_in_postgres(db_cursor):
    db_cursor.execute("SELECT indexname FROM pg_indexes WHERE indexname = 'metatest_name_idx'")
    assert db_cursor.fetchone() is not None


def test_the_unique_together_is_actually_enforced_by_postgres():
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
