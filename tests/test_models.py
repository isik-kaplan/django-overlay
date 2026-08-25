import re

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.test import override_settings
from django.test.utils import isolate_apps
from hypothesis import given
from hypothesis import strategies as st

from django_overlay.models import (
    OverlayConfigurationError,
    OverlayMeta,
    OverlayModel,
    OverlayModelBase,
    _default_soft_delete,
    _default_strategy,
    _split_meta_options,
)
from django_overlay.sources import SourceTable
from django_overlay.strategies import Strategy
from tests.testapp.models import Address, AddressNote, Person, SoftDeleteTest


def test_view_model_is_unmanaged_and_bound_to_the_view_table():
    assert Person._meta.managed is False
    assert Person._meta.db_table == "person_view"
    assert Person._is_overlay_view_model is True


def test_base_table_is_managed_and_bound_to_the_declared_table_name():
    base = Person.base_table()
    assert base._meta.managed is True
    assert base._meta.db_table == "person"
    assert base.__name__ == "PersonBase"


def test_base_model_points_back_to_the_view_model():
    assert Person.base_table()._view_model is Person


def test_both_models_share_the_declared_non_m2m_fields():
    view_fields = {f.name for f in Person._meta.fields}
    base_fields = {f.name for f in Person.base_table()._meta.fields}

    assert view_fields == {"id", "first_name", "age"}
    # The base model carries one extra: soft delete's shadow flag, which is
    # never exposed on the view and so never queryable through the model.
    assert base_fields == view_fields | {"_overlay_deleted"}


def test_m2m_fields_only_exist_on_the_view_model_not_the_base_model():
    view_m2m_names = {f.name for f in Person._meta.local_many_to_many}
    assert view_m2m_names == {"addresses", "phones"}
    assert Person.base_table()._meta.local_many_to_many == []


def test_field_instances_are_not_shared_between_view_and_base_model():
    # Field objects record a back-reference to their owning model, so
    # reusing one across two models corrupts whichever is set second.
    view_field = Person._meta.get_field("first_name")
    base_field = Person.base_table()._meta.get_field("first_name")
    assert view_field is not base_field
    assert view_field.model is Person
    assert base_field.model is Person.base_table()


def test_get_source_reads_from_the_declared_overlay_meta():
    source = Address.get_source()
    assert source.schema == "public"
    assert source.table == "testapp_shared_addresssource"


def test_both_models_share_the_metaclass():
    assert isinstance(Person, OverlayModelBase)
    assert isinstance(Person.base_table(), OverlayModelBase)


def test_overlay_foreign_key_targets_the_view_model():
    field = AddressNote._meta.get_field("address")
    assert field.related_model is Address


def test_person_uses_the_negative_id_strategy():
    assert Person._overlay_meta.strategy is Strategy.NEGATIVE_ID


def test_overlaymeta_not_subclassing_the_base_class_is_rejected():
    with pytest.raises(OverlayConfigurationError, match="must subclass"):

        class NotAnOverlay(OverlayModel):
            class OverlayMeta:
                table_name = "not_an_overlay"

                @staticmethod
                def get_source():
                    # Any source will do: these models are declared to exercise
                    # the metaclass, never migrated, so nothing reads the table.
                    return SourceTable(schema="public", table="testapp_shared_personsource")


def test_overlaymeta_without_get_source_override_is_rejected():
    with pytest.raises(OverlayConfigurationError, match="get_source"):

        class NoSources(OverlayModel):
            class OverlayMeta(OverlayMeta):
                table_name = "no_sources"


def test_a_source_less_overlay_model_is_rejected():
    """An overlay model with no source is a view over one table, three
    INSTEAD OF triggers routing writes straight back to it, and a tombstone
    column that can never be set — soft delete is decided per row, and a row
    with nothing to mask is hard deleted. The uniqueness machinery degenerates
    the same way: the source-side check has no source to check.

    A plain models.Model is the thing being asked for, and OverlayForeignKey
    works from one, so nothing is lost by refusing."""
    with pytest.raises(OverlayConfigurationError, match="nothing to overlay"):

        class Sourceless(OverlayModel):
            class OverlayMeta(OverlayMeta):
                table_name = "sourceless"

                @staticmethod
                def get_source():
                    return None


def test_the_refusal_says_what_to_write_instead():
    """A rejection that does not name the alternative just moves the problem."""
    with pytest.raises(OverlayConfigurationError) as raised:

        class AlsoSourceless(OverlayModel):
            class OverlayMeta(OverlayMeta):
                table_name = "also_sourceless"

                @staticmethod
                def get_source():
                    return None

    message = str(raised.value)
    assert "models.Model" in message
    assert "OverlayForeignKey" in message


def test_default_strategy_falls_back_to_uuid4_when_unconfigured():
    assert _default_strategy() is Strategy.UUID4


def test_default_strategy_reads_from_settings_when_configured():
    with override_settings(DJANGO_OVERLAY_DEFAULT_STRATEGY=Strategy.NEGATIVE_ID):
        assert _default_strategy() is Strategy.NEGATIVE_ID


def test_default_strategy_rejects_a_non_strategy_value():
    # Compared whole. Matching the setting name alone left the rest of the
    # sentence unpinned -- and the rest is what says a Strategy member is
    # wanted and gives an example of one, which is the actionable part.
    with override_settings(DJANGO_OVERLAY_DEFAULT_STRATEGY="negative_id"):
        with pytest.raises(ImproperlyConfigured) as raised:
            _default_strategy()

    assert str(raised.value) == (
        "settings.DJANGO_OVERLAY_DEFAULT_STRATEGY must be a django_overlay.strategies.Strategy "
        "member (e.g. Strategy.NEGATIVE_ID), got 'negative_id'."
    )


def test_overlay_meta_strategy_rejects_a_non_strategy_value():
    with pytest.raises(OverlayConfigurationError, match="strategy"):

        class BadStrategy(OverlayModel):
            class Meta:
                app_label = "testapp"

            class OverlayMeta(OverlayMeta):
                strategy = "uuid4"

                @staticmethod
                def get_source():
                    # Any source will do: these models are declared to exercise
                    # the metaclass, never migrated, so nothing reads the table.
                    return SourceTable(schema="public", table="testapp_shared_personsource")


def test_overlay_meta_soft_delete_rejects_a_non_bool_value():
    with pytest.raises(OverlayConfigurationError, match="soft_delete"):

        class BadSoftDelete(OverlayModel):
            class Meta:
                app_label = "testapp"

            class OverlayMeta(OverlayMeta):
                soft_delete = "false"

                @staticmethod
                def get_source():
                    # Any source will do: these models are declared to exercise
                    # the metaclass, never migrated, so nothing reads the table.
                    return SourceTable(schema="public", table="testapp_shared_personsource")


_non_strategy_value = st.one_of(
    st.text(), st.integers(), st.floats(allow_nan=False), st.none(), st.lists(st.integers())
)


@given(value=_non_strategy_value)
def test_overlay_meta_strategy_rejects_any_non_strategy_value(value):
    with pytest.raises(OverlayConfigurationError, match="strategy"):

        class BadStrategyFuzzed(OverlayModel):
            class Meta:
                app_label = "testapp"

            class OverlayMeta(OverlayMeta):
                strategy = value

                @staticmethod
                def get_source():
                    # Any source will do: these models are declared to exercise
                    # the metaclass, never migrated, so nothing reads the table.
                    return SourceTable(schema="public", table="testapp_shared_personsource")


_non_bool_value = st.one_of(st.text(), st.integers(), st.floats(allow_nan=False), st.none(), st.lists(st.integers()))


@given(value=_non_bool_value)
def test_overlay_meta_soft_delete_rejects_any_non_bool_value(value):
    with pytest.raises(OverlayConfigurationError, match="soft_delete"):

        class BadSoftDeleteFuzzed(OverlayModel):
            class Meta:
                app_label = "testapp"

            class OverlayMeta(OverlayMeta):
                soft_delete = value

                @staticmethod
                def get_source():
                    # Any source will do: these models are declared to exercise
                    # the metaclass, never migrated, so nothing reads the table.
                    return SourceTable(schema="public", table="testapp_shared_personsource")


def test_meta_db_table_is_rejected():
    with pytest.raises(OverlayConfigurationError, match="db_table"):

        class HasDbTable(OverlayModel):
            class Meta:
                db_table = "my_custom_table_name"

            class OverlayMeta(OverlayMeta):
                table_name = "has_db_table"

                @staticmethod
                def get_source():
                    # Any source will do: these models are declared to exercise
                    # the metaclass, never migrated, so nothing reads the table.
                    return SourceTable(schema="public", table="testapp_shared_personsource")


def test_meta_managed_is_rejected():
    with pytest.raises(OverlayConfigurationError, match="managed"):

        class HasManaged(OverlayModel):
            class Meta:
                managed = True

            class OverlayMeta(OverlayMeta):
                table_name = "has_managed"

                @staticmethod
                def get_source():
                    # Any source will do: these models are declared to exercise
                    # the metaclass, never migrated, so nothing reads the table.
                    return SourceTable(schema="public", table="testapp_shared_personsource")


def test_meta_permissions_is_rejected():
    # Matches the explanation, not just the option name: the reason is the part
    # a reader needs, and asserting only "permissions" leaves it unpinned.
    with pytest.raises(OverlayConfigurationError, match="permissions isn't supported on an OverlayModel"):

        class HasPermissions(OverlayModel):
            class Meta:
                permissions = [("can_do_x", "Can do x")]

            class OverlayMeta(OverlayMeta):
                table_name = "has_permissions"

                @staticmethod
                def get_source():
                    # Any source will do: these models are declared to exercise
                    # the metaclass, never migrated, so nothing reads the table.
                    return SourceTable(schema="public", table="testapp_shared_personsource")


def test_meta_default_permissions_is_rejected():
    with pytest.raises(
        OverlayConfigurationError,
        match=re.escape(
            "default_permissions isn't supported on an OverlayModel — there's no model to attach "
            "it to that makes sense (see _UNSUPPORTED_META_OPTIONS)."
        ),
    ):

        class HasDefaultPermissions(OverlayModel):
            class Meta:
                default_permissions = ()

            class OverlayMeta(OverlayMeta):
                table_name = "has_default_permissions"

                @staticmethod
                def get_source():
                    # Any source will do: these models are declared to exercise
                    # the metaclass, never migrated, so nothing reads the table.
                    return SourceTable(schema="public", table="testapp_shared_personsource")


def test_base_model_gets_no_default_permissions():
    assert Person.base_table()._meta.default_permissions == ()


def test_a_user_declared_id_field_is_not_overridden_by_the_strategy_default():
    class HasCustomId(OverlayModel):
        id = models.CharField(primary_key=True, max_length=20)

        class Meta:
            app_label = "testapp"

        class OverlayMeta(OverlayMeta):  # defaults to UUID4, which would inject a UUIDField
            table_name = "has_custom_id"

            @staticmethod
            def get_source():
                return SourceTable(schema="public", table="testapp_shared_personsource")

    assert isinstance(HasCustomId._meta.get_field("id"), models.CharField)


def test_meta_app_label_reaches_both_the_base_and_view_model():
    class HasExplicitAppLabel(OverlayModel):
        class Meta:
            app_label = "testapp"

        class OverlayMeta(OverlayMeta):
            table_name = "has_explicit_app_label"

            @staticmethod
            def get_source():
                return SourceTable(schema="public", table="testapp_shared_personsource")

    assert HasExplicitAppLabel._meta.app_label == "testapp"
    assert HasExplicitAppLabel.base_table()._meta.app_label == "testapp"


def test_default_soft_delete_defaults_to_true_when_unconfigured():
    assert _default_soft_delete() is True


def test_default_soft_delete_reads_from_settings_when_configured():
    with override_settings(DJANGO_OVERLAY_DEFAULT_SOFT_DELETE=False):
        assert _default_soft_delete() is False


def test_default_soft_delete_rejects_a_non_bool_value():
    with override_settings(DJANGO_OVERLAY_DEFAULT_SOFT_DELETE="yes"):
        with pytest.raises(ImproperlyConfigured, match="DJANGO_OVERLAY_DEFAULT_SOFT_DELETE"):
            _default_soft_delete()


def test_soft_delete_model_gets_a_base_only_shadow_field():
    assert "_overlay_deleted" in {f.name for f in SoftDeleteTest.base_table()._meta.fields}
    assert "_overlay_deleted" not in {f.name for f in SoftDeleteTest._meta.fields}


def test_declaring_your_own_overlay_deleted_field_is_rejected():
    with pytest.raises(OverlayConfigurationError, match="_overlay_deleted"):

        class HasOwnShadowField(OverlayModel):
            _overlay_deleted = models.CharField(max_length=1)

            class Meta:
                app_label = "testapp"

            class OverlayMeta(OverlayMeta):
                table_name = "has_own_shadow_field"
                soft_delete = True

                @staticmethod
                def get_source():
                    # Any source will do: these models are declared to exercise
                    # the metaclass, never migrated, so nothing reads the table.
                    return SourceTable(schema="public", table="testapp_shared_personsource")


def test_abstract_overlaymodel_subclass_is_not_split():
    class AbstractOverlay(OverlayModel):
        class Meta:
            abstract = True

    assert AbstractOverlay._meta.abstract is True
    assert not hasattr(AbstractOverlay, "_base_model")
    assert not hasattr(AbstractOverlay, "_overlay_meta")


def test_private_meta_attributes_are_not_forwarded_as_options():
    """`vars(Meta)` carries __module__, __qualname__ and friends; forwarding
    those as model options would either be ignored silently or blow up
    somewhere far from the cause."""
    base_options, view_options = _split_meta_options("Probe", type("Meta", (), {"ordering": ["x"]}))

    assert view_options == {"ordering": ["x"]}
    assert base_options == {}
    assert not any(key.startswith("_") for key in {**base_options, **view_options})


def test_multi_table_inheritance_from_a_concrete_overlay_model_is_rejected_by_name():
    """The message has to name the real problem. Before this it complained
    about a missing OverlayMeta, which sent you off writing one for a model
    that could never work."""
    with pytest.raises(OverlayConfigurationError, match="Multi-table inheritance isn't supported"):

        class Child(Person):
            extra = models.CharField(max_length=10)


@isolate_apps("tests.testapp")
def test_an_abstract_overlay_base_is_still_allowed():
    """The escape hatch the message points at. isolate_apps because this one
    actually builds — left in the real registry it would be a managed model
    whose table no migration ever created."""

    class Shared(OverlayModel):
        nickname = models.CharField(max_length=10)

        class Meta:
            abstract = True
            app_label = "testapp"

    class Concrete(Shared):
        class Meta:
            app_label = "testapp"

        class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
            table_name = "abstract_base_child"

            @staticmethod
            def get_source():
                return SourceTable(schema="public", table="testapp_shared_personsource")

    assert Concrete._meta.get_field("nickname") is not None
    assert Concrete.base_table()._meta.db_table == "abstract_base_child"
