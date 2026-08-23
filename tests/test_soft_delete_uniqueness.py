"""A soft-deleted row must stop reserving its unique values.

The tombstone stays in the base table forever — that's how masking works — so
every uniqueness rule on a soft_delete model is emitted as a *partial* index
(`WHERE NOT _overlay_deleted`), and the source-side trigger skips source rows a
tombstone is masking. See django_overlay/uniqueness.py.
"""

import pytest

from django_overlay.sources import SourceTable
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.test.utils import isolate_apps

from django_overlay.checks import uniqueness_error, unsupported_uniqueness
from django_overlay.constraints import OverlayUniqueConstraint
from django_overlay.models import OverlayMeta, OverlayModel
from django_overlay.uniqueness import constraint_name
from tests.testapp.models import (
    SoftDeletePlainUniqueTest,
    SoftDeleteUniqueTest,
    UniqueTestNoSource,
    Vendor,
)
from tests.testapp_shared.models import SoftDeleteUniqueTestSource


pytestmark = pytest.mark.django_db


def make(**kwargs):
    kwargs.setdefault("ssn", "ssn-default")
    kwargs.setdefault("email", "email-default")
    kwargs.setdefault("first_name", "first-default")
    kwargs.setdefault("last_name", "last-default")
    return SoftDeleteUniqueTest.objects.create(**kwargs)


# --------------------------------------------------------------- declaration


def _uniqueness_problems(meta_attrs=None, field_kwargs=None, soft_delete=True, extra_fields=None):
    """Build a throwaway OverlayModel and run the uniqueness check over it.

    The check lives in checks.py and runs from AppConfig.ready(), not from the
    metaclass, so the model builds fine and the complaint comes afterwards —
    which is also what makes it reachable from a test at all.

    Every caller is wrapped in isolate_apps: without it these throwaway models
    stay in the real app registry, and one with a relation to a real model
    sends Django's delete collector looking for a table that was never
    created."""
    _uniqueness_problems.counter += 1
    meta = type("Meta", (), {"app_label": "testapp", **(meta_attrs or {})})
    overlay_meta = type(
        "OverlayMeta",
        (OverlayMeta,),
        {"table_name": "probe_banned", "soft_delete": soft_delete, "get_source": staticmethod(lambda: SourceTable(schema="public", table="testapp_shared_personsource"))},
    )
    model = type(
        f"ProbeBanned{_uniqueness_problems.counter}",
        (OverlayModel,),
        {
            "__module__": "tests.testapp.models",
            "email": models.CharField(max_length=100, **(field_kwargs or {})),
            "first_name": models.CharField(max_length=100),
            "last_name": models.CharField(max_length=100),
            **(extra_fields or {}),
            "Meta": meta,
            "OverlayMeta": overlay_meta,
        },
    )
    return model, unsupported_uniqueness(model)


_uniqueness_problems.counter = 0


def _message(model, problems):
    error = uniqueness_error(model, problems)
    return f"{error.msg}\n\n{error.hint}"


def test_every_uniqueness_rule_becomes_a_partial_constraint():
    constraints = {c.name: c for c in SoftDeleteUniqueTest._base_model._meta.constraints}

    assert set(constraints) == {
        "softdeleteuniquetest_ssn_unique",
        "softdeleteuniquetest_first_name_last_name_uniq",
        "softdeleteuniquetest_email_uniq",
    }
    for constraint in constraints.values():
        assert constraint.condition == models.Q(_overlay_deleted=False), constraint.name


@isolate_apps("tests.testapp")
def test_unique_together_is_rejected_with_the_constraint_to_write_instead():
    model, problems = _uniqueness_problems(meta_attrs={"unique_together": [("first_name", "last_name")]})

    message = _message(model, problems)
    assert "- Meta.unique_together = ['first_name', 'last_name']" in message
    assert (
        'OverlayUniqueConstraint(fields=["first_name", "last_name"], name="probe_banned_first_name_last_name_uniq")'
        in message
    )


@isolate_apps("tests.testapp")
def test_field_level_unique_is_rejected_with_the_constraint_to_write_instead():
    model, problems = _uniqueness_problems(field_kwargs={"unique": True})

    message = _message(model, problems)
    assert "- email declares unique=True" in message
    assert 'OverlayUniqueConstraint(fields=["email"], name="probe_banned_email_uniq")' in message


@isolate_apps("tests.testapp")
def test_a_single_string_in_unique_together_is_handled():
    model, problems = _uniqueness_problems(meta_attrs={"unique_together": ["email"]})

    assert 'OverlayUniqueConstraint(fields=["email"], name="probe_banned_email_uniq")' in _message(model, problems)


@isolate_apps("tests.testapp")
def test_every_violation_is_reported_in_one_error():
    """Three separate boot failures to fix one model would be miserable."""
    model, problems = _uniqueness_problems(
        meta_attrs={"unique_together": [("first_name", "last_name")]},
        field_kwargs={"unique": True},
        extra_fields={"desk": models.OneToOneField("testapp.Vendor", on_delete=models.CASCADE, null=True)},
    )

    message = _message(model, problems)
    assert message.count("  - ") == 3
    for field in ("first_name", "email", "desk"):
        assert f'"{field}"' in message


@isolate_apps("tests.testapp")
def test_the_whole_error_is_exactly_what_the_developer_reads():
    """Every sentence of it, not a phrase from it.

    Fourteen mutants lived inside this message while the assertions around it
    picked out substrings: capitalisation, sentence boundaries, and the newline
    joining one complaint to the next. A developer hitting this at boot reads
    all of it, so all of it is asserted -- and with two problems rather than
    one, so the joins have something to join.
    """
    model, problems = _uniqueness_problems(
        meta_attrs={"unique_together": [("first_name", "last_name")]},
        field_kwargs={"unique": True},
    )
    error = uniqueness_error(model, problems)

    assert error.id == "django_overlay.E003"
    assert error.obj is model
    assert error.msg == (
        f"{model.__name__} declares uniqueness django_overlay can't honour:\n"
        "\n"
        "  - Meta.unique_together = ['first_name', 'last_name']\n"
        "  - email declares unique=True\n"
        "\n"
        "An overlay model is queried through a view spanning your table and the source "
        "table, so uniqueness has to hold across both. Every one of the above compiles "
        "down to a single index on your table alone, which would accept a value that "
        "already exists in the source. OverlayUniqueConstraint adds the source-side check."
    )
    assert error.hint == (
        "Declare them as OverlayUniqueConstraint in Meta.constraints instead:\n"
        "\n"
        "    constraints = [\n"
        '        OverlayUniqueConstraint(fields=["first_name", "last_name"], '
        'name="probe_banned_first_name_last_name_uniq"),\n'
        '        OverlayUniqueConstraint(fields=["email"], name="probe_banned_email_uniq"),\n'
        "    ]\n"
        "\n"
        "Those names are the ones django_overlay would have generated; any name that's "
        "unique across your models will do."
    )


@isolate_apps("tests.testapp")
def test_the_conditional_paragraph_is_exactly_what_it_says():
    """The extra paragraph is appended only for conditions, and it too was
    four mutants deep in unasserted prose."""
    model, problems = _uniqueness_problems(
        meta_attrs={
            "constraints": [
                models.UniqueConstraint(fields=["email"], condition=models.Q(email__gt=""), name="probe_cond")
            ]
        }
    )
    hint = uniqueness_error(model, problems).hint

    assert hint.endswith(
        "\n"
        "\n"
        "Conditional uniqueness isn't supported at all: the source-side trigger has no "
        "way to apply the condition, so it would check for collisions the condition "
        "should have excluded. If you genuinely want a condition over your own rows "
        "only, add the partial index by hand in a RunSQL migration and leave it out of "
        "Meta."
    )


@isolate_apps("tests.testapp")
def test_the_rejection_applies_without_soft_delete_too():
    # Nothing to do with tombstones: a plain unique index never covers the
    # source table, whether or not the model soft-deletes.
    _, problems = _uniqueness_problems(field_kwargs={"unique": True}, soft_delete=False)

    assert problems


@isolate_apps("tests.testapp")
def test_a_plain_unique_constraint_is_rejected():
    model, problems = _uniqueness_problems(
        meta_attrs={"constraints": [models.UniqueConstraint(fields=["email"], name="probe_plain")]}
    )

    message = _message(model, problems)
    assert "Meta.constraints has a plain UniqueConstraint 'probe_plain'" in message
    # The name they already chose is kept — only the class changes.
    assert 'OverlayUniqueConstraint(fields=["email"], name="probe_plain")' in message


@isolate_apps("tests.testapp")
def test_a_conditional_unique_constraint_says_conditions_are_unsupported():
    model, problems = _uniqueness_problems(
        meta_attrs={
            "constraints": [
                models.UniqueConstraint(fields=["email"], condition=models.Q(email__gt=""), name="probe_cond")
            ]
        }
    )

    message = _message(model, problems)
    assert "(with a condition)" in message
    assert "Conditional uniqueness isn't supported at all" in message
    assert "RunSQL migration" in message


@isolate_apps("tests.testapp")
def test_a_check_constraint_is_not_flagged():
    _, problems = _uniqueness_problems(
        meta_attrs={"constraints": [models.CheckConstraint(condition=models.Q(email__gt=""), name="probe_check")]}
    )

    assert problems == []


@isolate_apps("tests.testapp")
def test_a_one_to_one_field_needs_its_uniqueness_spelled_out():
    model, problems = _uniqueness_problems(
        extra_fields={"desk": models.OneToOneField("testapp.Vendor", on_delete=models.CASCADE, null=True)}
    )

    message = _message(model, problems)
    assert "desk is a OneToOneField, whose implicit uniqueness covers your table only" in message
    assert 'OverlayUniqueConstraint(fields=["desk"], name="probe_banned_desk_uniq")' in message


@isolate_apps("tests.testapp")
def test_a_one_to_one_field_with_the_constraint_is_accepted():
    _, problems = _uniqueness_problems(
        extra_fields={"desk": models.OneToOneField("testapp.Vendor", on_delete=models.CASCADE, null=True)},
        meta_attrs={"constraints": [OverlayUniqueConstraint(fields=["desk"], name="probe_desk_uniq")]},
    )

    assert problems == []


@isolate_apps("tests.testapp")
def test_a_composite_constraint_does_not_satisfy_a_one_to_one_field():
    """(desk, floor) being unique together says nothing about desk on its own,
    so it can't stand in for the field's implicit uniqueness."""
    model, problems = _uniqueness_problems(
        extra_fields={
            "desk": models.OneToOneField("testapp.Vendor", on_delete=models.CASCADE, null=True),
            "floor": models.IntegerField(default=0),
        },
        meta_attrs={"constraints": [OverlayUniqueConstraint(fields=["desk", "floor"], name="probe_composite")]},
    )

    assert "desk is a OneToOneField" in _message(model, problems)


@isolate_apps("tests.testapp")
def test_the_one_to_one_requirement_applies_without_soft_delete_too():
    _, problems = _uniqueness_problems(
        extra_fields={"desk": models.OneToOneField("testapp.Vendor", on_delete=models.CASCADE, null=True)},
        soft_delete=False,
    )

    assert problems


def test_the_one_to_one_field_survives_on_the_view_model():
    # The whole point of not making people downgrade it to a ForeignKey.
    field = SoftDeletePlainUniqueTest._meta.get_field("vendor")

    assert isinstance(field, models.OneToOneField)
    assert field.one_to_one
    assert Vendor._meta.get_field("plain_thing").one_to_many is False


def test_the_base_model_gets_the_plain_foreign_key_underneath():
    base_field = SoftDeletePlainUniqueTest._base_model._meta.get_field("vendor")

    assert type(base_field) is models.ForeignKey
    assert not base_field.unique  # no table constraint, so the constraint below can be partial


def test_the_one_to_one_uniqueness_comes_from_the_constraint():
    constraint = _plain_constraint("softdeleteplainuniquetest_vendor_uniq")

    assert constraint.condition == models.Q(_overlay_deleted=False)


def test_a_one_to_one_value_is_freed_by_a_soft_delete():
    vendor = Vendor.objects.create(name="Acme")
    SoftDeletePlainUniqueTest.objects.create(code="o1", vendor=vendor).delete()

    SoftDeletePlainUniqueTest.objects.create(code="o2", vendor=vendor)


def test_a_live_one_to_one_duplicate_is_still_rejected():
    vendor = Vendor.objects.create(name="Acme")
    SoftDeletePlainUniqueTest.objects.create(code="o3", vendor=vendor)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            SoftDeletePlainUniqueTest.objects.create(code="o4", vendor=vendor)


def test_a_model_without_soft_delete_is_left_alone():
    # UniqueTestNoSource sets soft_delete = False explicitly; soft delete is
    # the default now, so an opted-out model is what proves the narrowing is
    # conditional rather than unconditional.
    constraint = UniqueTestNoSource._base_model._meta.constraints[0]

    assert constraint.condition is None
    assert constraint.soft_delete is False


def test_the_overlay_constraint_survives_a_deconstruct_round_trip():
    original = OverlayUniqueConstraint(fields=["ssn"], name="x", soft_delete=True)

    path, args, kwargs = original.deconstruct()
    rebuilt = OverlayUniqueConstraint(*args, **kwargs)

    assert "condition" not in kwargs, "condition= would be rejected on the way back in"
    assert rebuilt.soft_delete is True
    assert rebuilt.condition == models.Q(_overlay_deleted=False)


def test_generated_names_are_stable_and_short():
    assert constraint_name("t", ("a", "b")) == "t_a_b_uniq"

    long_name = constraint_name("a" * 60, ("b" * 60,))
    assert len(long_name) <= 63
    assert long_name == constraint_name("a" * 60, ("b" * 60,))


def test_a_name_of_exactly_the_limit_is_left_alone():
    """Boundary: 63 characters is legal, 64 is not."""
    table = "t" * (63 - len("_f_uniq"))

    name = constraint_name(table, ("f",))

    assert len(name) == 63
    assert name == f"{table}_f_uniq", "a name that already fits must not be hashed"


def test_a_name_over_the_limit_is_truncated_to_exactly_the_limit():
    name = constraint_name("t" * 80, ("f",))

    assert len(name) == 63, "the hash suffix has to leave room for itself and the separator"
    assert name[-9] == "_"


# ------------------------------------------------------------------ behaviour


def test_the_index_in_postgres_is_partial(db_cursor):
    db_cursor.execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'softdeleteuniquetest' AND indexname = %s",
        ["softdeleteuniquetest_email_uniq"],
    )

    assert "WHERE (NOT _overlay_deleted)" in db_cursor.fetchone()[0]


def test_a_locally_created_unique_value_is_freed_by_a_soft_delete():
    make(email="taken@example.com").delete()

    make(email="taken@example.com")


def test_a_unique_together_value_is_freed_by_a_soft_delete():
    make(first_name="Ada", last_name="Lovelace").delete()

    make(first_name="Ada", last_name="Lovelace")


def test_an_overlay_unique_value_is_freed_by_a_soft_delete():
    make(ssn="111-11").delete()

    make(ssn="111-11")


def test_a_masked_source_rows_value_is_freed_too(db_cursor):
    """The partial index does nothing here — the value lives in the source
    table, and only the trigger's tombstone check frees it."""
    source = SoftDeleteUniqueTestSource.objects.create(
        ssn="222-22", email="src@example.com", first_name="Src", last_name="Row"
    )
    SoftDeleteUniqueTest.objects.filter(pk=-source.id).delete()

    with transaction.atomic():
        make(ssn="222-22", email="new@example.com")
        db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_a_visible_source_value_is_still_reserved(db_cursor):
    SoftDeleteUniqueTestSource.objects.create(
        ssn="333-33", email="visible@example.com", first_name="Vis", last_name="Row"
    )

    with pytest.raises(IntegrityError, match="overlay unique violation"):
        with transaction.atomic():
            make(ssn="333-33")
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_a_live_duplicate_is_still_rejected():
    make(email="live@example.com")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make(email="live@example.com")


def test_a_live_unique_together_duplicate_is_still_rejected():
    make(first_name="Grace", last_name="Hopper")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make(first_name="Grace", last_name="Hopper")


def test_restoring_a_masked_row_takes_its_value_back(db_cursor):
    source = SoftDeleteUniqueTestSource.objects.create(
        ssn="444-44", email="back@example.com", first_name="Back", last_name="Again"
    )
    SoftDeleteUniqueTest.objects.filter(pk=-source.id).delete()
    SoftDeleteUniqueTest(pk=-source.id).reset_to_source()

    with pytest.raises(IntegrityError, match="overlay unique violation"):
        with transaction.atomic():
            make(ssn="444-44")
            db_cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_validation_now_agrees_with_the_database():
    """The disagreement this whole change exists to remove: full_clean()
    queries the view, the index covers the table."""
    make(email="agree@example.com", ssn="555-55", first_name="A", last_name="B").delete()

    candidate = SoftDeleteUniqueTest(email="agree@example.com", ssn="555-55", first_name="A", last_name="B")
    candidate.full_clean()
    candidate.save()


def test_validation_still_rejects_a_live_duplicate():
    make(email="dup@example.com")

    with pytest.raises(ValidationError) as exc_info:
        SoftDeleteUniqueTest(email="dup@example.com", ssn="x", first_name="y", last_name="z").full_clean()

    assert "email" in exc_info.value.message_dict


# ------------------------------------------------- plain Django constraints


def test_a_plain_unique_constraint_is_narrowed():
    constraint = _plain_constraint("softdeleteplainuniquetest_code")

    assert constraint.condition == models.Q(_overlay_deleted=False)


def test_a_check_constraint_is_left_alone():
    constraint = _plain_constraint("softdeleteplainuniquetest_code_not_empty")

    assert constraint.condition == models.Q(code__gt="")


def test_validation_sees_the_un_narrowed_copies():
    # _overlay_deleted is base-only, so a validation constraint carrying it
    # would blow up resolving the field on the view model.
    conditions = [c.condition for _, cs in SoftDeletePlainUniqueTest(code="x").get_constraints() for c in cs]

    assert models.Q(_overlay_deleted=False) not in conditions


def test_a_plain_unique_value_is_freed_by_a_soft_delete():
    SoftDeletePlainUniqueTest.objects.create(code="c1").delete()

    SoftDeletePlainUniqueTest.objects.create(code="c1")


def test_a_live_plain_duplicate_is_still_rejected():
    SoftDeletePlainUniqueTest.objects.create(code="c4")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            SoftDeletePlainUniqueTest.objects.create(code="c4")


def test_the_check_constraint_is_still_enforced():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            SoftDeletePlainUniqueTest.objects.create(code="")


def _plain_constraint(name):
    return next(c for c in SoftDeletePlainUniqueTest._base_model._meta.constraints if c.name == name)


def test_narrowing_round_trips_exactly():
    """The narrowed and un-narrowed forms are derived from each other, so
    there's one source of truth rather than two lists to keep in step."""
    declared = OverlayUniqueConstraint(fields=["ssn"], name="rt")

    narrowed = SoftDeleteUniqueTest._base_model._meta.constraints[0]
    unnarrowed = narrowed.without_soft_delete_narrowing()

    assert declared.soft_delete is False and declared.condition is None
    assert narrowed.soft_delete is True
    assert narrowed.condition == models.Q(_overlay_deleted=False)
    assert unnarrowed.soft_delete is False
    assert unnarrowed.condition is None
    assert unnarrowed.fields == narrowed.fields
    assert unnarrowed.name == narrowed.name


def test_un_narrowing_a_plain_constraint_is_a_no_op():
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="plain")

    assert constraint.without_soft_delete_narrowing() is constraint


def test_validation_constraints_are_derived_not_stored():
    # No second list on the model to drift out of step with the real one.
    assert not hasattr(SoftDeleteUniqueTest, "_validation_constraints")

    ddl = {c.name for c in SoftDeleteUniqueTest._base_model._meta.constraints}
    validated = {c.name for _, cs in SoftDeleteUniqueTest(ssn="x").get_constraints() for c in cs}

    assert ddl == validated


@isolate_apps("tests.testapp")
def test_the_registered_check_collects_an_error_per_bad_model(monkeypatch):
    """The scan-and-collect half of check_overlay_uniqueness. The models the
    real registry holds are all valid, so the bad one is fed in directly —
    isolate_apps keeps it out of the global registry, which is also why the
    check can't find it on its own."""
    from django_overlay import checks as overlay_checks

    bad, _ = _uniqueness_problems(field_kwargs={"unique": True})
    good = SoftDeleteUniqueTest
    monkeypatch.setattr(overlay_checks.apps, "get_models", lambda: [good, bad, Vendor])

    errors = overlay_checks.check_overlay_uniqueness(None)

    assert [error.id for error in errors] == ["django_overlay.E003"]
    assert errors[0].obj is bad
    assert "email declares unique=True" in errors[0].msg
    assert "OverlayUniqueConstraint" in errors[0].hint


def test_the_registered_check_is_clean_for_the_real_registry():
    from django_overlay import checks as overlay_checks

    assert overlay_checks.check_overlay_uniqueness(None) == []


@isolate_apps("tests.testapp")
def test_scanning_does_not_stop_at_the_first_acceptable_constraint():
    """A `break` where the loop wants `continue` would report only what comes
    before the first OverlayUniqueConstraint."""
    model, problems = _uniqueness_problems(
        meta_attrs={
            "constraints": [
                OverlayUniqueConstraint(fields=["first_name"], name="probe_ok"),
                models.UniqueConstraint(fields=["email"], name="probe_bad"),
            ]
        }
    )

    assert [complaint for complaint, _, _ in problems] == ["Meta.constraints has a plain UniqueConstraint 'probe_bad'"]


@isolate_apps("tests.testapp")
def test_scanning_does_not_stop_at_a_check_constraint():
    model, problems = _uniqueness_problems(
        meta_attrs={
            "constraints": [
                models.CheckConstraint(condition=models.Q(email__gt=""), name="probe_check"),
                models.UniqueConstraint(fields=["email"], name="probe_bad"),
            ]
        }
    )

    assert [complaint for complaint, _, _ in problems] == ["Meta.constraints has a plain UniqueConstraint 'probe_bad'"]


@isolate_apps("tests.testapp")
def test_the_conditional_note_is_appended_to_the_complaint_not_substituted():
    _, problems = _uniqueness_problems(
        meta_attrs={
            "constraints": [
                models.UniqueConstraint(fields=["email"], condition=models.Q(email__gt=""), name="probe_cond")
            ]
        }
    )

    assert problems[0][0] == "Meta.constraints has a plain UniqueConstraint 'probe_cond' (with a condition)"


def test_null_values_do_not_collide_under_an_overlay_unique_constraint():
    """SQL treats NULLs as non-colliding, and the partial unique index has to
    agree — otherwise a single nullable column could only ever be empty on one
    row. `vendor` is the OneToOneField, so this is the constraint that replaced
    its implicit uniqueness."""
    SoftDeletePlainUniqueTest.objects.create(code="n1", vendor=None)
    SoftDeletePlainUniqueTest.objects.create(code="n2", vendor=None)

    assert SoftDeletePlainUniqueTest.objects.filter(vendor__isnull=True).count() == 2


def test_a_null_still_does_not_excuse_a_collision_on_another_column():
    SoftDeletePlainUniqueTest.objects.create(code="dup", vendor=None)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            SoftDeletePlainUniqueTest.objects.create(code="dup", vendor=None)


def test_a_condition_on_the_overlay_constraint_itself_is_refused():
    """The constraint's own refusal, which nothing had ever read.

    Seven mutants lived in this message. It is the one telling an author why
    the thing they wrote cannot work, so a garbled version is worse than none.
    """
    from django_overlay.exceptions import OverlayConfigurationError

    with pytest.raises(OverlayConfigurationError) as raised:
        OverlayUniqueConstraint(fields=["ssn"], name="n", condition=models.Q(ssn__gt=""))

    assert str(raised.value) == (
        "OverlayUniqueConstraint doesn't support condition= — the source-vs-base trigger "
        "has no way to apply it, so it would silently check for collisions the condition "
        "should have excluded."
    )


def test_a_soft_delete_condition_is_not_mistaken_for_a_caller_supplied_one():
    """The flag sets a condition internally, after the check above."""
    constraint = OverlayUniqueConstraint(fields=["ssn"], name="n", soft_delete=True)

    assert constraint.condition == models.Q(_overlay_deleted=False)
    assert constraint.soft_delete is True


def test_an_expression_constraint_keeps_its_expressions():
    """UniqueConstraint takes expressions positionally, and they ride in *args.

    Nothing here had ever built one that way, so both places that forward
    *args -- the constructor and without_soft_delete_narrowing -- could drop
    them and every test still passed, while in practice the constraint would
    silently become one over no columns at all.
    """
    from django.db.models.functions import Lower

    constraint = OverlayUniqueConstraint(Lower("ssn"), name="expr_constraint")

    assert constraint.deconstruct()[1] == (Lower("ssn"),)


def test_dropping_the_narrowing_keeps_the_expressions_too():
    from django.db.models.functions import Lower

    narrowed = OverlayUniqueConstraint(Lower("ssn"), name="expr_constraint", soft_delete=True)
    plain = narrowed.without_soft_delete_narrowing()

    assert plain.deconstruct()[1] == (Lower("ssn"),)
    assert plain.condition is None
