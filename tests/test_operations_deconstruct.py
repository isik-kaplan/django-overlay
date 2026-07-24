from django_overlay.operations import (
    AddOverlayConstraint,
    AddOverlayUniqueConstraint,
    RemoveOverlayConstraint,
    RemoveOverlayUniqueConstraint,
    SyncOverlayView,
)


def test_sync_overlay_view_deconstructs_to_its_constructor_args():
    name, args, kwargs = SyncOverlayView("testapp", "Person").deconstruct()
    assert name == "SyncOverlayView"
    assert args == ["testapp", "Person"]
    assert kwargs == {}


def test_add_overlay_constraint_deconstructs_to_its_constructor_args():
    name, args, kwargs = AddOverlayConstraint("testapp", "AddressNote", "address").deconstruct()
    assert name == "AddOverlayConstraint"
    assert args == ["testapp", "AddressNote", "address"]
    assert kwargs == {}


def test_add_overlay_unique_constraint_deconstructs_to_its_constructor_args():
    name, args, kwargs = AddOverlayUniqueConstraint("testapp", "UniqueTest", "uniquetest_ssn_unique").deconstruct()
    assert name == "AddOverlayUniqueConstraint"
    assert args == ["testapp", "UniqueTest", "uniquetest_ssn_unique"]
    assert kwargs == {}


def test_remove_overlay_constraint_omits_column_kwarg_when_using_the_default_convention():
    name, args, kwargs = RemoveOverlayConstraint("testapp", "AddressNote", "address").deconstruct()
    assert name == "RemoveOverlayConstraint"
    assert args == ["testapp", "AddressNote", "address"]
    assert kwargs == {}


def test_remove_overlay_constraint_keeps_column_kwarg_when_it_was_customized():
    name, args, kwargs = RemoveOverlayConstraint("testapp", "AddressNote", "address", column="custom_col").deconstruct()
    assert kwargs == {"column": "custom_col"}


def test_remove_overlay_unique_constraint_deconstructs_to_its_constructor_args():
    name, args, kwargs = RemoveOverlayUniqueConstraint("testapp", "UniqueTest", "uniquetest_ssn_unique").deconstruct()
    assert name == "RemoveOverlayUniqueConstraint"
    assert args == ["testapp", "UniqueTest", "uniquetest_ssn_unique"]
    assert kwargs == {}
