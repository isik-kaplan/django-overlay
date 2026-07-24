import uuid

from django.db import models

from django_overlay.models import OverlayMeta, OverlayModel
from django_overlay.strategies import Strategy, default_id_field, default_pk_sql, negates_source_ids
from django_overlay.uuid7 import uuid7


def test_strategy_is_exposed_on_overlaymodel_and_overlaymeta():
    assert OverlayModel.Strategy is Strategy
    assert OverlayMeta.Strategy is Strategy


def test_overlaymeta_defaults_to_uuid4():
    assert OverlayMeta.strategy is Strategy.UUID4


def test_with_strategy_returns_a_subclass_configured_for_that_strategy():
    configured = OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)
    assert issubclass(configured, OverlayMeta)
    assert configured.strategy is Strategy.NEGATIVE_ID
    assert OverlayMeta.strategy is Strategy.UUID4  # base class untouched


def test_negative_id_strategy_negates_source_ids_and_injects_no_field():
    assert negates_source_ids(Strategy.NEGATIVE_ID) is True
    assert default_id_field(Strategy.NEGATIVE_ID) is None
    assert default_pk_sql(Strategy.NEGATIVE_ID) is None


def test_uuid_strategies_do_not_negate_source_ids():
    assert negates_source_ids(Strategy.UUID4) is False
    assert negates_source_ids(Strategy.UUID7) is False
    assert negates_source_ids(Strategy.UUID7_POLYFILL) is False


def test_uuid4_strategy_injects_a_uuidfield_defaulting_to_uuid4():
    field = default_id_field(Strategy.UUID4)
    assert isinstance(field, models.UUIDField)
    assert field.default is uuid.uuid4
    assert default_pk_sql(Strategy.UUID4) == "gen_random_uuid()"


def test_uuid7_strategy_injects_a_uuidfield_defaulting_to_our_uuid7():
    field = default_id_field(Strategy.UUID7)
    assert isinstance(field, models.UUIDField)
    assert field.default is uuid7
    assert default_pk_sql(Strategy.UUID7) == "uuidv7()"


def test_uuid7_polyfill_strategy_uses_the_same_field_but_a_portable_sql_default():
    field = default_id_field(Strategy.UUID7_POLYFILL)
    assert isinstance(field, models.UUIDField)
    assert field.default is uuid7
    sql = default_pk_sql(Strategy.UUID7_POLYFILL)
    assert "gen_random_uuid()" in sql
    assert "uuidv7" not in sql


def test_uuid7_polyfill_produces_a_fresh_field_instance_each_call():
    assert default_id_field(Strategy.UUID4) is not default_id_field(Strategy.UUID4)
