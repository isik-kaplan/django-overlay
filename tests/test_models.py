import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.test import override_settings

from django_overlay.models import (
    OverlayConfigurationError,
    OverlayMeta,
    OverlayModel,
    OverlayModelBase,
    _default_soft_delete,
    _default_strategy,
)
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
    assert view_fields == base_fields == {"id", "first_name", "age"}


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
                    return None


def test_overlaymeta_without_get_source_override_is_rejected():
    with pytest.raises(OverlayConfigurationError, match="get_source"):

        class NoSources(OverlayModel):
            class OverlayMeta(OverlayMeta):
                table_name = "no_sources"


def test_default_strategy_falls_back_to_uuid4_when_unconfigured():
    assert _default_strategy() is Strategy.UUID4


def test_default_strategy_reads_from_settings_when_configured():
    with override_settings(DJANGO_OVERLAY_DEFAULT_STRATEGY=Strategy.NEGATIVE_ID):
        assert _default_strategy() is Strategy.NEGATIVE_ID


def test_default_strategy_rejects_a_non_strategy_value():
    with override_settings(DJANGO_OVERLAY_DEFAULT_STRATEGY="negative_id"):
        with pytest.raises(ImproperlyConfigured, match="DJANGO_OVERLAY_DEFAULT_STRATEGY"):
            _default_strategy()


def test_overlay_meta_strategy_rejects_a_non_strategy_value():
    with pytest.raises(OverlayConfigurationError, match="strategy"):

        class BadStrategy(OverlayModel):
            class Meta:
                app_label = "testapp"

            class OverlayMeta(OverlayMeta):
                strategy = "uuid4"

                @staticmethod
                def get_source():
                    return None


def test_overlay_meta_soft_delete_rejects_a_non_bool_value():
    with pytest.raises(OverlayConfigurationError, match="soft_delete"):

        class BadSoftDelete(OverlayModel):
            class Meta:
                app_label = "testapp"

            class OverlayMeta(OverlayMeta):
                soft_delete = "false"

                @staticmethod
                def get_source():
                    return None


def test_meta_db_table_is_rejected():
    with pytest.raises(OverlayConfigurationError, match="db_table"):

        class HasDbTable(OverlayModel):
            class Meta:
                db_table = "my_custom_table_name"

            class OverlayMeta(OverlayMeta):
                table_name = "has_db_table"

                @staticmethod
                def get_source():
                    return None


def test_meta_managed_is_rejected():
    with pytest.raises(OverlayConfigurationError, match="managed"):

        class HasManaged(OverlayModel):
            class Meta:
                managed = True

            class OverlayMeta(OverlayMeta):
                table_name = "has_managed"

                @staticmethod
                def get_source():
                    return None


def test_meta_permissions_is_rejected():
    with pytest.raises(OverlayConfigurationError, match="permissions"):

        class HasPermissions(OverlayModel):
            class Meta:
                permissions = [("can_do_x", "Can do x")]

            class OverlayMeta(OverlayMeta):
                table_name = "has_permissions"

                @staticmethod
                def get_source():
                    return None


def test_meta_default_permissions_is_rejected():
    with pytest.raises(OverlayConfigurationError, match="default_permissions"):

        class HasDefaultPermissions(OverlayModel):
            class Meta:
                default_permissions = ()

            class OverlayMeta(OverlayMeta):
                table_name = "has_default_permissions"

                @staticmethod
                def get_source():
                    return None


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
                return None

    assert isinstance(HasCustomId._meta.get_field("id"), models.CharField)


def test_meta_app_label_reaches_both_the_base_and_view_model():
    class HasExplicitAppLabel(OverlayModel):
        class Meta:
            app_label = "testapp"

        class OverlayMeta(OverlayMeta):
            table_name = "has_explicit_app_label"

            @staticmethod
            def get_source():
                return None

    assert HasExplicitAppLabel._meta.app_label == "testapp"
    assert HasExplicitAppLabel.base_table()._meta.app_label == "testapp"


def test_default_soft_delete_defaults_to_false_when_unconfigured():
    assert _default_soft_delete() is False


def test_default_soft_delete_reads_from_settings_when_configured():
    with override_settings(DJANGO_OVERLAY_DEFAULT_SOFT_DELETE=True):
        assert _default_soft_delete() is True


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
                    return None


def test_abstract_overlaymodel_subclass_is_not_split():
    class AbstractOverlay(OverlayModel):
        class Meta:
            abstract = True

    assert AbstractOverlay._meta.abstract is True
    assert not hasattr(AbstractOverlay, "_base_model")
    assert not hasattr(AbstractOverlay, "_overlay_meta")
