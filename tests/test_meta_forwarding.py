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


def test_the_constraint_is_actually_enforced_by_postgres():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            MetaTest.objects.create(name="")


def test_ordering_is_honored_on_the_view_models_default_queryset():
    MetaTest.objects.create(name="Zoe")
    MetaTest.objects.create(name="Amy")
    assert list(MetaTest.objects.values_list("name", flat=True)) == ["Amy", "Zoe"]
