from django_overlay.fields import OverlayForeignKey, OverlayOneToOneField
from tests.testapp.models import Address, AddressNote, Person, PersonProfile


def test_overlay_foreign_key_never_creates_a_db_constraint():
    field = AddressNote._meta.get_field("address")
    assert field.db_constraint is False


def test_deconstruct_does_not_leak_db_constraint_as_an_explicit_kwarg():
    field = AddressNote._meta.get_field("address")
    _, _, _, kwargs = field.deconstruct()
    assert "db_constraint" not in kwargs


def test_trigger_name_is_stable_and_within_postgres_identifier_length():
    field = AddressNote._meta.get_field("address")
    name = field.trigger_name(AddressNote)
    assert name == "overlayfk_testapp_addressnote_address_id"
    assert len(name) <= 63


def test_target_tables_include_the_base_table_and_every_declared_source():
    field = AddressNote._meta.get_field("address")
    targets = field.target_tables()
    tables = [t for t, _, _ in targets]
    assert Address.base_table()._meta.db_table in tables
    assert '"public"."testapp_shared_addresssource"' in tables


def test_target_tables_only_negate_the_source_side_for_a_negative_id_strategy_target():
    field = AddressNote._meta.get_field("address")
    targets = field.target_tables()
    negate_by_table = {t: negate for t, _, negate in targets}
    assert negate_by_table[Address.base_table()._meta.db_table] is False
    assert negate_by_table['"public"."testapp_shared_addresssource"'] is True


def test_overlay_one_to_one_field_is_also_an_overlay_foreign_key():
    field = PersonProfile._meta.get_field("person")
    assert isinstance(field, OverlayForeignKey)
    assert isinstance(field, OverlayOneToOneField)
    assert field.db_constraint is False
    assert field.one_to_one is True


def test_overlay_one_to_one_field_target_tables_point_at_person():
    targets = PersonProfile._meta.get_field("person").target_tables()
    tables = [t for t, _, _ in targets]
    assert Person.base_table()._meta.db_table in tables
    assert '"public"."testapp_shared_personsource"' in tables
