"""Fields an overlay model inherits from an abstract parent get the same
treatment as fields declared in its body, and a primary key, declared or
inherited, has to suit the id strategy."""

import uuid

import pytest
from django.db import models
from django.test.utils import isolate_apps

from django_overlay.exceptions import OverlayConfigurationError
from django_overlay.fields import OverlayOneToOneField
from django_overlay.models import OverlayMeta, OverlayModel
from django_overlay.sources import SourceTable
from django_overlay.strategies import Strategy
from django_overlay.uuid7 import uuid7


def _overlay_meta(strategy=Strategy.UUID7, **attrs):
    """An OverlayMeta class; get_source has to sit in its own __dict__."""

    def get_source():
        return SourceTable(schema="public", table="testapp_shared_personsource")

    return type(
        "OverlayMeta", (OverlayMeta.with_strategy(strategy),), {"get_source": staticmethod(get_source), **attrs}
    )


def _declare(name, parent, strategy=Strategy.UUID7, body=None, **meta_attrs):
    attrs = {
        "__module__": __name__,
        "Meta": type("Meta", (), {"app_label": "testapp"}),
        "OverlayMeta": _overlay_meta(strategy, table_name=name.lower(), **meta_attrs),
        **(body or {}),
    }
    return type(name, (parent,), attrs)


def _abstract(name, **fields):
    return type(
        name,
        (OverlayModel,),
        {"__module__": __name__, "Meta": type("Meta", (), {"abstract": True, "app_label": "testapp"}), **fields},
    )


def _commented_uuid_pk():
    return models.UUIDField(primary_key=True, default=uuid7, editable=False, help_text="h", db_comment="c")


@isolate_apps("tests.testapp")
def test_an_inherited_primary_key_is_kept_with_its_comment():
    parent = _abstract("Stamped", id=_commented_uuid_pk())
    model = _declare("Inherits", parent)

    for half in (model, model.base_table()):
        pk = half._meta.pk
        assert (pk.name, pk.help_text, pk.db_comment) == ("id", "h", "c")
    assert model._meta.pk is not model.base_table()._meta.pk


@isolate_apps("tests.testapp")
def test_an_inherited_primary_key_under_another_name_stops_the_injection():
    parent = _abstract("Keyed", code=models.UUIDField(primary_key=True, default=uuid7))
    model = _declare("InheritsCode", parent)

    assert model._meta.pk.name == "code"
    assert "id" not in {field.name for field in model.base_table()._meta.fields}


@isolate_apps("tests.testapp")
def test_the_body_overrides_an_inherited_field():
    parent = _abstract("Named", nickname=models.CharField(max_length=10))
    model = _declare("Renames", parent, body={"nickname": models.CharField(max_length=99)})

    assert model._meta.get_field("nickname").max_length == 99
    assert model.base_table()._meta.get_field("nickname").max_length == 99


@isolate_apps("tests.testapp")
def test_an_inherited_relation_hides_its_reverse_side_on_the_base_model():
    target = type(
        "Owner", (models.Model,), {"__module__": __name__, "Meta": type("Meta", (), {"app_label": "testapp"})}
    )
    parent = _abstract("Owned", owner=models.ForeignKey(target, models.CASCADE, related_name="things"))
    model = _declare("Thing", parent)

    assert model.base_table()._meta.get_field("owner").remote_field.related_name == "+"
    assert model._meta.get_field("owner").remote_field.related_name == "things"
    assert not [error for error in model.check() + model.base_table().check() if error.id.startswith("fields.E3")]


@isolate_apps("tests.testapp")
def test_an_inherited_one_to_one_is_a_plain_foreign_key_on_the_base_model():
    target = type(
        "Profile", (models.Model,), {"__module__": __name__, "Meta": type("Meta", (), {"app_label": "testapp"})}
    )
    parent = _abstract("HasProfile", profile=OverlayOneToOneField(target, models.CASCADE))
    model = _declare("Account", parent)

    base_field = model.base_table()._meta.get_field("profile")
    assert not base_field.one_to_one and not base_field.unique
    assert model._meta.get_field("profile").one_to_one


@isolate_apps("tests.testapp")
def test_an_inherited_many_to_many_is_on_the_view_model_only():
    target = type("Tag", (models.Model,), {"__module__": __name__, "Meta": type("Meta", (), {"app_label": "testapp"})})
    parent = _abstract("Tagged", tags=models.ManyToManyField(target, related_name="+"))
    model = _declare("Post", parent)

    assert model._meta.get_field("tags").many_to_many
    assert "tags" not in {field.name for field in model.base_table()._meta.get_fields()}


@isolate_apps("tests.testapp")
def test_an_inherited_overlay_deleted_field_is_refused():
    parent = _abstract("Reserves", _overlay_deleted=models.BooleanField(default=False))

    with pytest.raises(OverlayConfigurationError, match="reserves that name"):
        _declare("Clash", parent)


@isolate_apps("tests.testapp")
@pytest.mark.parametrize(
    ("strategy", "pk"),
    [
        (Strategy.UUID7, models.AutoField(primary_key=True)),
        (Strategy.UUID4, models.BigIntegerField(primary_key=True)),
        (Strategy.NEGATIVE_ID, models.UUIDField(primary_key=True, default=uuid.uuid4)),
        (Strategy.NEGATIVE_SOURCE_POSITIVE_BASE_ID, models.CharField(primary_key=True, max_length=10)),
    ],
)
def test_a_primary_key_the_strategy_cannot_fill_is_refused(strategy, pk):
    with pytest.raises(OverlayConfigurationError, match=r"needs (a UUIDField|an integer field) as the primary key"):
        _declare("Mismatch", OverlayModel, strategy, body={"id": pk})


@isolate_apps("tests.testapp")
def test_an_inherited_primary_key_the_strategy_cannot_fill_is_refused():
    parent = _abstract("IntKeyed", id=models.BigAutoField(primary_key=True))

    with pytest.raises(OverlayConfigurationError, match="BigAutoField, but Strategy.UUID7 needs a UUIDField"):
        _declare("InheritsInt", parent)


@isolate_apps("tests.testapp")
@pytest.mark.parametrize(
    ("strategy", "pk"),
    [
        (Strategy.UUID7_POLYFILL, models.UUIDField(primary_key=True, default=uuid7)),
        (Strategy.NEGATIVE_ID, models.BigAutoField(primary_key=True)),
        (Strategy.NEGATIVE_BASE_POSITIVE_SOURCE_ID, models.IntegerField(primary_key=True)),
    ],
)
def test_a_primary_key_that_suits_the_strategy_is_accepted(strategy, pk):
    assert _declare("Match", OverlayModel, strategy, body={"id": pk})._meta.pk.name == "id"


@isolate_apps("tests.testapp")
def test_pk_default_sql_takes_the_primary_key_out_of_the_check():
    model = _declare(
        "OwnDefault",
        OverlayModel,
        Strategy.UUID7,
        body={"id": models.CharField(primary_key=True, max_length=36)},
        pk_default_sql="gen_random_uuid()::text",
    )

    assert isinstance(model._meta.pk, models.CharField)


@isolate_apps("tests.testapp")
def test_a_primary_key_that_is_a_link_is_not_checked():
    target = type(
        "Parent", (models.Model,), {"__module__": __name__, "Meta": type("Meta", (), {"app_label": "testapp"})}
    )
    model = _declare(
        "KeyedOnLink",
        OverlayModel,
        Strategy.UUID7,
        body={"parent": OverlayOneToOneField(target, models.CASCADE, primary_key=True)},
    )

    assert model._meta.pk.name == "parent"


@isolate_apps("tests.testapp")
def test_an_id_that_is_not_the_primary_key_is_left_for_django_to_refuse():
    """Nothing is injected over it: replacing a field the model declared would
    hide the mistake, and Django already reports it as models.E004."""
    model = _declare("NotKeyedOnId", OverlayModel, Strategy.UUID7, body={"id": models.IntegerField()})

    assert "models.E004" in {error.id for error in model.base_table().check()}


@isolate_apps("tests.testapp")
def test_the_refusal_names_the_field():
    with pytest.raises(OverlayConfigurationError, match=r"^Named\.code is a CharField"):
        _declare("Named", OverlayModel, body={"code": models.CharField(primary_key=True, max_length=10)})
