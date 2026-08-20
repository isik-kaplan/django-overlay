"""Can a unique value be freed by a soft delete?

Two independent blockers, proved separately against real Postgres:

  (a) the base table's unique index still contains the tombstone row
  (b) the source-side trigger still sees the source row the tombstone masks

SoftDeleteTest (soft_delete=True, source-backed) stands in for a model with an
OverlayUniqueConstraint on `first_name`; the index and trigger are built here
by hand so the two candidate fixes can be switched on and off independently.

    POSTGRES_USER=postgres uv run pytest tests/probe_unique_after_soft_delete.py \
        -s -q -p no:cacheprovider --no-cov
"""

import pytest
from django.db import IntegrityError, connection, transaction

from tests.testapp.models import SoftDeleteTest
from tests.testapp_shared.models import SoftDeleteTestSource


pytestmark = pytest.mark.django_db(transaction=True)

RESULTS = []

SOURCE = "testapp_shared_softdeletetestsource"
BASE = "softdeletetest"

# The trigger as it ships today: any source row with a matching value blocks,
# whether or not a tombstone is masking it.
TRIGGER_TODAY = f"""
CREATE OR REPLACE FUNCTION probe_unique_check() RETURNS TRIGGER AS $$
BEGIN
  IF (TG_OP = 'INSERT' OR NEW.first_name IS DISTINCT FROM OLD.first_name)
     AND NEW.first_name IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM {SOURCE}
       WHERE first_name = NEW.first_name
         AND id != -NEW.id
     )
  THEN
    RAISE EXCEPTION 'overlay unique violation' USING ERRCODE = '23505';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# Candidate fix (b): a source row masked by a tombstone is not a visible row,
# so its value must not reserve anything.
TRIGGER_MASK_AWARE = f"""
CREATE OR REPLACE FUNCTION probe_unique_check() RETURNS TRIGGER AS $$
BEGIN
  IF (TG_OP = 'INSERT' OR NEW.first_name IS DISTINCT FROM OLD.first_name)
     AND NEW.first_name IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM {SOURCE} s
       WHERE s.first_name = NEW.first_name
         AND s.id != -NEW.id
         AND NOT EXISTS (
           SELECT 1 FROM {BASE} b
           WHERE b.id = -s.id AND b._overlay_deleted
         )
     )
  THEN
    RAISE EXCEPTION 'overlay unique violation' USING ERRCODE = '23505';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def setup(cursor, *, partial_index: bool, mask_aware_trigger: bool):
    cursor.execute(f"DROP TRIGGER IF EXISTS probe_unique_trigger ON {BASE}")
    cursor.execute(f"DELETE FROM {BASE}")
    cursor.execute(f"DELETE FROM {SOURCE}")
    cursor.execute("DROP INDEX IF EXISTS probe_unique_idx")
    predicate = " WHERE NOT _overlay_deleted" if partial_index else ""
    cursor.execute(f"CREATE UNIQUE INDEX probe_unique_idx ON {BASE} (first_name){predicate}")
    cursor.execute(TRIGGER_MASK_AWARE if mask_aware_trigger else TRIGGER_TODAY)
    cursor.execute(f"DROP TRIGGER IF EXISTS probe_unique_trigger ON {BASE}")
    cursor.execute(
        f"CREATE CONSTRAINT TRIGGER probe_unique_trigger AFTER INSERT OR UPDATE ON {BASE} "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION probe_unique_check()"
    )


def attempt(fn):
    """True if it worked, False if the database refused. Runs as a real
    transaction so the deferred trigger fires at a real COMMIT."""
    try:
        with transaction.atomic():
            fn()
        return True
    except IntegrityError:
        return False


def check(name, expected, actual):
    RESULTS.append((name, "OK" if expected == actual else "FAIL", f"expected {expected}, got {actual}"))


def test_freeing_a_unique_value_with_a_soft_delete():
    with connection.cursor() as cursor:
        try:
            # --- (a) a locally-created row, no source involved -----------------
            for partial in (False, True):
                setup(cursor, partial_index=partial, mask_aware_trigger=False)
                obj = SoftDeleteTest.objects.create(first_name="local")
                obj.delete()
                reused = attempt(lambda: SoftDeleteTest.objects.create(first_name="local"))
                check(f"reuse an organic row's value       (partial index={partial})", partial, reused)

            # --- (b) a source-backed row, masked by a tombstone ----------------
            for mask_aware in (False, True):
                setup(cursor, partial_index=True, mask_aware_trigger=mask_aware)
                src = SoftDeleteTestSource.objects.create(first_name="from-source")
                SoftDeleteTest.objects.filter(pk=-src.id).delete()
                reused = attempt(lambda: SoftDeleteTest.objects.create(first_name="from-source"))
                check(f"reuse a masked source row's value  (mask-aware trigger={mask_aware})", mask_aware, reused)

            # --- both fixes on: real collisions must still be refused ----------
            setup(cursor, partial_index=True, mask_aware_trigger=True)
            SoftDeleteTest.objects.create(first_name="live")
            check(
                "a live local duplicate is still refused",
                False,
                attempt(lambda: SoftDeleteTest.objects.create(first_name="live")),
            )

            setup(cursor, partial_index=True, mask_aware_trigger=True)
            SoftDeleteTestSource.objects.create(first_name="visible-source")
            check(
                "a visible source collision is still refused",
                False,
                attempt(lambda: SoftDeleteTest.objects.create(first_name="visible-source")),
            )

            # --- and the value must go back to being taken if the row returns --
            setup(cursor, partial_index=True, mask_aware_trigger=True)
            src = SoftDeleteTestSource.objects.create(first_name="restored")
            SoftDeleteTest.objects.filter(pk=-src.id).delete()
            SoftDeleteTest(pk=-src.id).reset_to_source()
            check(
                "un-masking re-reserves the value",
                False,
                attempt(lambda: SoftDeleteTest.objects.create(first_name="restored")),
            )

            # --- does validation agree with the database now? ------------------
            setup(cursor, partial_index=True, mask_aware_trigger=True)
            obj = SoftDeleteTest.objects.create(first_name="agreement")
            obj.delete()
            visible = SoftDeleteTest.objects.filter(first_name="agreement").exists()
            accepted = attempt(lambda: SoftDeleteTest.objects.create(first_name="agreement"))
            check("view visibility and the index now agree", not visible, accepted)

        finally:
            cursor.execute("DROP TRIGGER IF EXISTS probe_unique_trigger ON " + BASE)
            cursor.execute("DROP FUNCTION IF EXISTS probe_unique_check()")
            cursor.execute("DROP INDEX IF EXISTS probe_unique_idx")
            cursor.execute(f"DELETE FROM {BASE}")
            cursor.execute(f"DELETE FROM {SOURCE}")
    width = max(len(n) for n, _, _ in RESULTS)
    print("\n\n=== freeing a unique value with a soft delete ===")
    for name, status, detail in RESULTS:
        print(f"{status:5} {name:<{width}}  {detail}")
    failed = [r for r in RESULTS if r[1] != "OK"]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} as predicted")
