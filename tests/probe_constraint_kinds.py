"""Does uniqueness cover the whole view, everywhere?

OverlayUniqueConstraint is now the only form allowed on an overlay model, so
every uniqueness rule should behave identically and cover base + source. This
probe used to compare it against a plain UniqueConstraint, which enforced only
half of that; the comparison is kept as a regression check that the two columns
below can never disagree again.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from tests.testapp.models import SoftDeleteUniqueTest
from tests.testapp_shared.models import SoftDeleteUniqueTestSource


pytestmark = pytest.mark.django_db

RESULTS = []


def record(name, outcome):
    RESULTS.append((name, outcome))


def db_accepts(**kwargs):
    kwargs.setdefault("ssn", "free-ssn")
    kwargs.setdefault("email", "free@x")
    kwargs.setdefault("first_name", "free")
    kwargs.setdefault("last_name", "free")
    try:
        with transaction.atomic():
            SoftDeleteUniqueTest.objects.create(**kwargs)
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        return "accepted"
    except IntegrityError:
        return "rejected"


def validation_accepts(**kwargs):
    kwargs.setdefault("ssn", "free-ssn")
    kwargs.setdefault("email", "free@x")
    kwargs.setdefault("first_name", "free")
    kwargs.setdefault("last_name", "free")
    try:
        SoftDeleteUniqueTest(**kwargs).full_clean()
        return "accepted"
    except ValidationError:
        return "rejected"


def test_constraint_kinds():
    # --- collision with a row that exists only in the SOURCE table ---------
    SoftDeleteUniqueTestSource.objects.create(ssn="src-ssn", email="src@x", first_name="Src", last_name="Only")
    record("ssn   (single-field), source collision ->  database", db_accepts(ssn="src-ssn"))
    record("ssn   (single-field), source collision ->  full_clean()", validation_accepts(ssn="src-ssn"))
    record("email (single-field), source collision ->  database", db_accepts(email="src@x"))
    record("email (single-field), source collision ->  full_clean()", validation_accepts(email="src@x"))

    # --- collision with a locally-created row ------------------------------
    SoftDeleteUniqueTest.objects.create(ssn="local-ssn", email="local@x", first_name="Local", last_name="Row")
    record("ssn   (single-field), local collision  ->  database", db_accepts(ssn="local-ssn"))
    record("ssn   (single-field), local collision  ->  full_clean()", validation_accepts(ssn="local-ssn"))
    record("email (single-field), local collision  ->  database", db_accepts(email="local@x"))
    record("email (single-field), local collision  ->  full_clean()", validation_accepts(email="local@x"))

    assert {outcome for _, outcome in RESULTS} == {"rejected"}, RESULTS
    width = max(len(n) for n, _ in RESULTS)
    print("\n\n=== every uniqueness rule covers base + source ===")
    for name, outcome in RESULTS:
        print(f"{name:<{width}}  {outcome}")
