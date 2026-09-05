"""Taking the lock and replacing the view.

Every test here builds a real second table, points a model at it and asks
Postgres what happened, because the failures this half exists to avoid are
exactly the ones that produce no error: a cutover that repoints the view and
leaves the triggers probing the table it came from is a database that answers
every query correctly and enforces nothing.
"""

from dataclasses import replace

import pytest
from django.db import IntegrityError, connection, connections, transaction

from django_overlay import sync as sync_module
from django_overlay.exceptions import OverlaySwapRefused
from django_overlay.sources import SourceTable
from django_overlay.swaps import (
    ERROR,
    ROW_CHECKS,
    WARNING,
    Finding,
    deployed_source,
    swap_source,
    verify_source_swap,
)
from django_overlay.swaps import cutover as swaps_cutover
from django_overlay.sync import resolve_schema, resync_view, sync_source_triggers
from tests.test_swaps.support import (
    _Rollback,
    a_populated_tenant,
    analyze,
    assert_header,
    assert_message,
    codes,
    green,
    point_at,
)
from tests.testapp.models import (
    Person,
    PersonProfile,
    UniqueTest,
)
from tests.testapp_shared.models import PersonSource, UniqueTestSource


pytestmark = pytest.mark.django_db


def test_deployed_source_reads_the_table_the_view_actually_uses():
    schema = resolve_schema(connection)
    assert deployed_source(connection, schema, UniqueTest) == SourceTable(
        schema="public", table="testapp_shared_uniquetestsource"
    )


def test_deployed_source_does_not_believe_get_source(monkeypatch, db_cursor):
    """The whole reason it introspects. Config naming green while the database
    still reads blue is not an error state, it is the middle of a swap."""
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    deployed = deployed_source(connection, resolve_schema(connection), UniqueTest)

    assert deployed.table == "testapp_shared_uniquetestsource"
    assert UniqueTest.get_source().table == "green_uniquetest"


def test_swapping_to_the_table_already_deployed_does_nothing():
    report = swap_source(UniqueTest, identity_columns=["ssn"])

    assert_header(
        report,
        "testapp.UniqueTest",
        "public.testapp_shared_uniquetestsource",
        "public.testapp_shared_uniquetestsource",
    )
    assert_message(
        report,
        "S018",
        WARNING,
        "uniquetest_view already reads public.testapp_shared_uniquetestsource. Nothing to swap — if "
        "you meant to change it, edit get_source() first.",
    )


def test_a_refused_swap_leaves_the_view_where_it_was(monkeypatch, db_cursor):
    UniqueTest.objects.create(ssn="222-22-2222")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, '222-22-2222', '')")
    # Without this the candidate is refused for being empty, which is a
    # refusal, so the test passed -- on a finding it was not written for and
    # does not mention.
    analyze(db_cursor, "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    with pytest.raises(OverlaySwapRefused) as raised:
        swap_source(UniqueTest, identity_columns=["ssn"])

    # The exception's own text is the report, not a summary of it. Most callers
    # will do no more than `except OverlaySwapRefused as exc: log(exc)`, and if
    # that prints anything less than the findings then the one thing that says
    # why a cutover was refused is the one thing that did not survive being
    # raised. `.report` is there for callers that want the structure.
    assert str(raised.value) == str(raised.value.report)
    assert "S009" in str(raised.value)

    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == (
        "testapp_shared_uniquetestsource"
    )


def test_a_dry_run_reports_and_changes_nothing(monkeypatch, db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    report = swap_source(UniqueTest, identity_columns=["ssn"], dry_run=True)

    assert report.ok
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == (
        "testapp_shared_uniquetestsource"
    )


@pytest.mark.django_db(databases=["default", "other"])
def test_a_swap_opens_its_transaction_on_the_database_it_was_given(monkeypatch):
    """The cutover is one transaction, and everything in it -- the lock, the
    recheck, the view, every trigger -- runs on `using`. A transaction opened
    on some other connection leaves all of that autocommitting one statement at
    a time: the lock is released as soon as it is taken, and a failure halfway
    leaves the view reading one table while its triggers probe another, which
    is the single state this function exists to make unreachable.

    On the second alias, because with one configured `using=None` and
    `using="default"` are the same call and nothing can tell them apart.
    """
    seen = {}
    real = swaps_cutover.sync_view

    def record(model, tenant_schema, execute, **kwargs):
        seen["savepoints"] = len(connections["other"].savepoint_ids)
        return real(model, tenant_schema, execute, **kwargs)

    with connections["other"].cursor() as cursor:
        try:
            with transaction.atomic(using="other"):
                outside = len(connections["other"].savepoint_ids)
                cursor.execute("INSERT INTO testapp_shared_uniquetestsource (id, ssn, notes) VALUES (1, '111', '')")
                candidate = green(cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
                point_at(monkeypatch, UniqueTest, candidate)
                monkeypatch.setattr(swaps_cutover, "sync_view", record)

                swap_source(UniqueTest, identity_columns=["ssn"], using="other")

                assert seen["savepoints"] == outside + 1
                raise _Rollback
        except _Rollback:
            pass


def test_a_swap_repoints_the_view(monkeypatch, db_cursor):
    db_cursor.execute("INSERT INTO testapp_shared_uniquetestsource (id, ssn, notes) VALUES (7001, 'blue', '')")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("UPDATE green_uniquetest SET notes = 'from-green' WHERE id = 7001")
    point_at(monkeypatch, UniqueTest, candidate)

    swap_source(UniqueTest, identity_columns=["ssn"])

    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == "green_uniquetest"
    assert UniqueTest.objects.get(pk=-7001).notes == "from-green"


def test_a_swap_repoints_the_uniqueness_trigger(monkeypatch, db_cursor):
    """The regression this whole change exists for.

    The uniqueness trigger's body names the source table as literal PL/pgSQL
    text. Replacing the view without replacing the trigger leaves the view
    reading green and the constraint that is supposed to guard it asking blue —
    so a value green already holds is accepted, and the view then returns two
    rows for a column declared unique."""
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, 'only-in-green', '')")
    analyze(db_cursor, "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    swap_source(UniqueTest, identity_columns=["ssn"])

    with pytest.raises(IntegrityError):
        UniqueTest.objects.create(ssn="only-in-green")


def test_a_swap_repoints_an_inbound_foreign_key_trigger(monkeypatch, db_cursor):
    """The other half of the same bug, from the other side. The FK's
    insert-side trigger lives on the *referencing* table and names the target's
    source, so it is invisible to anything that only looks at the model being
    swapped."""
    PersonSource.objects.create(first_name="Jane")
    candidate = green(db_cursor, "testapp_shared_personsource", "green_person")
    db_cursor.execute("INSERT INTO green_person (id, first_name, age) VALUES (9001, 'Only In Green', NULL)")
    analyze(db_cursor, "green_person")
    point_at(monkeypatch, Person, candidate)

    swap_source(Person, identity_columns=["first_name"])

    with transaction.atomic():
        PersonProfile.objects.create(person_id=-9001, bio="")
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_resync_view_repoints_the_uniqueness_trigger(monkeypatch, db_cursor):
    """resync_overlay_views is what the docs point at for a source change, so
    it has to do the whole job on its own — not just the half swap_source()
    remembers to finish."""
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, 'only-in-green', '')")
    point_at(monkeypatch, UniqueTest, candidate)

    resync_view(UniqueTest)

    with pytest.raises(IntegrityError):
        UniqueTest.objects.create(ssn="only-in-green")


def statements_for(model, tenant_schema="public"):
    collected = []
    sync_source_triggers(model, tenant_schema, collected.append)
    return collected


def test_a_rebuilt_uniqueness_trigger_carries_this_models_own_rules(monkeypatch, db_cursor):
    """Its name, its source, its id strategy and its soft-delete narrowing.

    The name is the one worth being explicit about: built under a different
    name, the rebuilt trigger is installed *beside* the old one instead of
    replacing it. Both fire, so the resync still looks like it worked, and the
    table is left carrying a trigger that probes the old source and that
    nothing will ever drop.
    """
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    sql = statements_for(UniqueTest)

    assert len(sql) == 1
    assert '"check_overlayunique_uniquetest_uniquetest_ssn_unique"' in sql[0]
    assert '"public"."green_uniquetest"' in sql[0]
    # NEGATIVE_ID, so a base row's own source origin is found by negating its
    # pk. Without it the row conflicts with itself on every write.
    assert 'src."id" != -NEW."id"' in sql[0]
    # soft_delete, so a source row a tombstone masks reserves nothing.
    assert "tombstone._overlay_deleted" in sql[0]


def test_a_rebuilt_foreign_key_trigger_keeps_both_halves_pruned(monkeypatch, db_cursor):
    """The insert side prunes by the column the referencing row carries, and
    the delete side by the one the target row carries -- two different columns,
    for the same partition key, because the two triggers fire on different
    tables. Lose either and the probe is still correct and scans every
    partition, which is the one failure that shows up as nothing but slowness.
    """
    partitioned = replace(Person.get_source(), partition_key="first_name")
    field = PersonProfile._meta.get_field("person")
    point_at(monkeypatch, Person, partitioned)
    monkeypatch.setattr(field, "partition_column", "bio")

    sql = statements_for(Person)

    insert_side = [s for s in sql if '"check_overlayfk_testapp_personprofile_person_id"' in s]
    delete_side = [s for s in sql if '"check_overlayfkdel_testapp_personprofile_person_id"' in s]
    assert len(insert_side) == 1 and len(delete_side) == 1
    assert '"first_name" = NEW."bio"' in insert_side[0]
    assert '"first_name" = OLD."first_name"' in delete_side[0]


def test_a_rebuilt_foreign_key_trigger_re_finds_its_row_by_the_referencing_key(monkeypatch):
    """The insert-side trigger is deferred, so at COMMIT it has to go back and
    find the row it fired for -- by the referencing model's own primary key.
    Every model in this suite happens to call that column `id`, which is
    exactly why passing it has to be asserted rather than assumed: a hardcoded
    default agrees with all of them and with no project that renamed it."""
    monkeypatch.setattr(PersonProfile._meta.pk, "column", "profile_key")

    sql = statements_for(Person)

    insert_side = [s for s in sql if '"check_overlayfk_testapp_personprofile_person_id"' in s][0]
    assert '"profile_key"' in insert_side


@pytest.mark.django_db(databases=["default", "other"])
def test_a_resync_opens_its_transaction_on_the_connection_it_writes_to(monkeypatch):
    """One transaction is the whole point of resync_view: Postgres does DDL
    transactionally, so readers see the old source or the new one and never a
    mixture. A transaction opened on a *different* connection from the one the
    statements run on is no transaction at all -- they autocommit one by one,
    and the window between replacing the view and replacing the triggers is
    open again, which is the state this function exists to make unreachable.
    """
    seen = {}
    real = sync_module.sync_view
    other = connections["other"]
    # The test case already holds a transaction open on every alias, so
    # `in_atomic_block` is True before resync_view is even called and says
    # nothing. What the transaction resync opens *on this connection* leaves
    # behind is a savepoint on it.
    outside = len(other.savepoint_ids)

    def record(model, tenant_schema, execute, **kwargs):
        seen["savepoints"] = len(other.savepoint_ids)
        return real(model, tenant_schema, execute, **kwargs)

    monkeypatch.setattr(sync_module, "sync_view", record)

    resync_view(UniqueTest, using="other")

    assert seen["savepoints"] == outside + 1


def test_a_swap_refused_under_the_lock_carries_the_findings_that_refused_it(monkeypatch, db_cursor):
    """The recheck runs with the lock held and after the preflight said yes, so
    its refusal is the one nobody is expecting. It has to arrive carrying what
    it found -- an exception raised at that point with an empty report says a
    cutover was abandoned and gives no reason at all."""
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    real = swaps_cutover.verify_source_swap
    calls = []

    def refuse_the_second_time(model, cand, **kwargs):
        calls.append(kwargs)
        report = real(model, cand, **kwargs)
        if len(calls) == 1:
            return report
        return replace(report, findings=(Finding("S007", ERROR, "a write landed while we waited."),))

    monkeypatch.setattr(swaps_cutover, "verify_source_swap", refuse_the_second_time)

    with pytest.raises(OverlaySwapRefused) as raised:
        swap_source(UniqueTest, identity_columns=["ssn"])

    assert "a write landed while we waited." in str(raised.value)
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == (
        "testapp_shared_uniquetestsource"
    )


def test_a_swap_can_cross_schemas(monkeypatch, db_cursor):
    """A blue-green source is often a whole schema rather than a table beside
    the old one. The deployed relation's schema and table are both carried
    over, and taking only the table would check the candidate against a table
    of the same name in the wrong schema -- which is a comparison that
    succeeds, against something nobody asked about."""
    UniqueTestSource.objects.create(ssn="111-11-1111")
    db_cursor.execute("DROP SCHEMA IF EXISTS vendor_green CASCADE")
    db_cursor.execute("CREATE SCHEMA vendor_green")
    db_cursor.execute(
        "CREATE TABLE vendor_green.testapp_shared_uniquetestsource "
        "(LIKE public.testapp_shared_uniquetestsource INCLUDING ALL)"
    )
    db_cursor.execute(
        "INSERT INTO vendor_green.testapp_shared_uniquetestsource SELECT * FROM public.testapp_shared_uniquetestsource"
    )
    db_cursor.execute("ANALYZE vendor_green.testapp_shared_uniquetestsource")
    elsewhere = replace(UniqueTest.get_source(), schema="vendor_green")
    point_at(monkeypatch, UniqueTest, elsewhere)

    report = swap_source(UniqueTest, identity_columns=["ssn"])

    # Same table name, different schema: the report has to say so on both sides
    # or it is not describing the swap that happened.
    assert_header(
        report,
        "testapp.UniqueTest",
        "public.testapp_shared_uniquetestsource",
        "vendor_green.testapp_shared_uniquetestsource",
    )
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).schema == "vendor_green"
    db_cursor.execute("DROP SCHEMA vendor_green CASCADE")


def test_a_finding_can_be_allowed_through(monkeypatch, db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    UniqueTest.objects.create(ssn="222-22-2222")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    db_cursor.execute("INSERT INTO green_uniquetest (id, ssn, notes) VALUES (9001, '222-22-2222', '')")
    analyze(db_cursor, "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    report = swap_source(UniqueTest, identity_columns=["ssn"], allow=["S009"])

    assert report.ok
    # Downgraded, not dropped: an accepted finding still has to be visible.
    assert "S009" in codes(report)
    assert "[allowed]" in str(report)
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == "green_uniquetest"


def test_the_source_to_check_against_can_be_given_explicitly(monkeypatch, db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    report = swap_source(
        UniqueTest,
        identity_columns=["ssn"],
        current=SourceTable(schema="public", table="testapp_shared_uniquetestsource"),
        dry_run=True,
    )

    assert report.ok
    assert report.current.table == "testapp_shared_uniquetestsource"


def test_a_view_that_is_not_deployed_cannot_be_swapped(monkeypatch, db_cursor):
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)
    db_cursor.execute("DROP VIEW public.uniquetest_view")

    assert deployed_source(connection, resolve_schema(connection), UniqueTest) is None
    with pytest.raises(OverlaySwapRefused) as refused:
        swap_source(UniqueTest, identity_columns=["ssn"])
    assert_header(refused.value.report, "testapp.UniqueTest", "(nothing deployed)", "public.green_uniquetest")
    assert_message(
        refused.value.report,
        "S017",
        ERROR,
        "Could not read a single source relation out of uniquetest_view. Either the view is not "
        "deployed (run migrations), or it reads something this package did not write. Pass "
        "current= to say what to check against.",
    )


def test_a_check_that_only_fails_under_the_lock_aborts_the_cutover(monkeypatch, db_cursor):
    """The preflight and the cutover are two moments, and a write can land
    between them. The re-run under the lock is what makes that survivable, so
    it has to actually stop the swap."""
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    # Patched on the module that *defines* it, not on the package that
    # re-exports it: swap_source() resolves the name as a global of its own
    # module, so patching the re-export would change nothing it reads.
    from django_overlay.swaps import cutover as swaps_module

    real = swaps_module.verify_source_swap
    calls = []

    def flaky(*args, **kwargs):
        report = real(*args, **kwargs)
        calls.append(report)
        if len(calls) == 1:
            return report
        return replace(report, findings=(Finding("S009", "error", "raced"),))

    monkeypatch.setattr(swaps_module, "verify_source_swap", flaky)

    with pytest.raises(OverlaySwapRefused):
        swap_source(UniqueTest, identity_columns=["ssn"])

    assert len(calls) == 2
    assert deployed_source(connection, resolve_schema(connection), UniqueTest).table == (
        "testapp_shared_uniquetestsource"
    )


def test_the_recheck_under_the_lock_asks_the_same_question_as_the_preflight(monkeypatch, db_cursor):
    """The re-run is only worth taking a lock for if it checks the same thing.
    Dropping the identity columns, or the source it compares against, would
    leave a cutover that verifies less at the moment it matters most and still
    reports the preflight's clean result."""
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")
    point_at(monkeypatch, UniqueTest, candidate)

    from django_overlay.swaps import cutover as swaps_module

    real = swaps_module.verify_source_swap
    calls = []

    def record(model, cand, **kwargs):
        calls.append(kwargs)
        return real(model, cand, **kwargs)

    monkeypatch.setattr(swaps_module, "verify_source_swap", record)
    swap_source(UniqueTest, identity_columns=["ssn"])

    preflight, recheck = calls
    assert recheck["current"] == preflight["current"]
    assert recheck["identity_columns"] == preflight["identity_columns"] == ["ssn"]
    assert recheck["using"] == preflight["using"] == "default"
    assert recheck["min_row_ratio"] == preflight["min_row_ratio"]
    # The shape half is the only thing the two differ on, and deliberately:
    # schema cannot change under the lock, rows can.
    assert preflight.get("checks") is None
    assert recheck["checks"] is ROW_CHECKS


def test_a_vendor_refresh_swaps_cleanly_over_a_tenant_who_has_been_using_it(monkeypatch, db_cursor):
    """The procedure, start to finish, on a tenant with something to lose.

    The vendor rebuilds the table: same ids meaning the same people, one
    person's details corrected, one person added. Every overlay semantic has to
    survive it -- an untouched row picks the refresh up, an overridden row does
    not, a tombstone still hides, a reference still resolves, and a row that
    only exists in the new table is simply there."""
    untouched, overridden, deleted = a_populated_tenant(db_cursor)
    candidate = green(db_cursor, "testapp_shared_personsource", "green_person")
    db_cursor.execute("UPDATE green_person SET age = 37 WHERE id = %s", [untouched.id])
    db_cursor.execute("UPDATE green_person SET age = 46 WHERE id = %s", [overridden.id])
    db_cursor.execute("INSERT INTO green_person (id, first_name, age) VALUES (9001, 'Katherine', 33)")
    analyze(db_cursor, "green_person")

    # Preflight while the old table is still the one being served.
    report = verify_source_swap(Person, candidate, identity_columns=["first_name"])
    assert report.ok, str(report)

    point_at(monkeypatch, Person, candidate)
    swap_source(Person, identity_columns=["first_name"])

    # An untouched row is a window onto the source, so it refreshes.
    assert Person.objects.get(pk=-untouched.id).age == 37
    # An overridden one is a copy, so it does not -- not the name the tenant
    # set, and not the age the vendor corrected underneath it either.
    assert Person.objects.get(pk=-overridden.id).first_name == "Grace H."
    assert Person.objects.get(pk=-overridden.id).age == 45
    # The tombstone still masks its row, which is only true if the swap kept
    # the id the tombstone was written against.
    assert not Person.objects.filter(pk=-deleted.id).exists()
    # And the reference still resolves, through the new table.
    assert PersonProfile.objects.get().person.first_name == "Ada"
    assert Person.objects.get(pk=-9001).first_name == "Katherine"


def test_a_swap_can_be_pointed_back_at_the_table_it_came_from(monkeypatch, db_cursor):
    """Rolling back is the same operation with the arguments the other way
    round, and the reason to keep the old table rather than drop it on success.

    Worth its own test because the second cutover is the one that runs against
    a database the first one already changed -- the deployed source it reads
    back is now the candidate of the previous swap."""
    untouched, overridden, _ = a_populated_tenant(db_cursor)
    blue = Person.get_source()
    candidate = green(db_cursor, "testapp_shared_personsource", "green_person")
    db_cursor.execute("UPDATE green_person SET age = 37 WHERE id = %s", [untouched.id])
    analyze(db_cursor, "green_person")

    point_at(monkeypatch, Person, candidate)
    swap_source(Person, identity_columns=["first_name"])
    assert Person.objects.get(pk=-untouched.id).age == 37

    point_at(monkeypatch, Person, blue)
    swap_source(Person, identity_columns=["first_name"])

    assert deployed_source(connection, resolve_schema(connection), Person).table == ("testapp_shared_personsource")
    assert Person.objects.get(pk=-untouched.id).age == 36
    assert Person.objects.get(pk=-overridden.id).first_name == "Grace H."
    assert PersonProfile.objects.get().person.first_name == "Ada"
