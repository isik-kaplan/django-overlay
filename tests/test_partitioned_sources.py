"""Declaring a source table partitioned, and what the package does with it.

A partitioned parent is transparent to correctness and opaque to cost. The same
probe returns the same rows whether the source is one table or fifty, and
without the partition key in its predicate Postgres plans an Append over every
partition -- right answer, fifty index scans. Ordinary application queries carry
the key because somebody wrote them. The triggers this package generates cannot,
so the source declares the key once and every probe that has the value in scope
uses it.

The SQL those templates produce is pinned in tests/test_sql.py. This file is
about the declaration: what it means, what has to line up for it to work, and
what `manage.py check` says when it doesn't.

The pairing that matters throughout is the negative one -- an undeclared key has
to leave everything exactly as it was, because that is what makes the feature
free for every project that does not partition.
"""

from unittest import mock

import pytest
from django.db import connection, models

from django_overlay.checks import _partition_column_problems, _partition_key_problems
from django_overlay.fields import OverlayForeignKey, target_tables_for
from django_overlay.introspection import partition_summary
from django_overlay.sources import SourceTable
from tests.testapp.models import Address, AddressNote, Person, PersonAddressThrough, PersonProfile


PARTITIONED = SourceTable(schema="public", table="testapp_shared_personsource", partition_key="first_name")
BAD_KEY = SourceTable(schema="public", table="testapp_shared_personsource", partition_key="not_a_column")


def partitioned(model, source):
    """`model.get_source()` returning `source` for the duration."""
    return mock.patch.object(model, "get_source", staticmethod(lambda: source))


def assert_problem(problems, problem_id, obj, message, hint):
    """The whole of one check result: its id, what it is about, what it says
    and what it tells you to do.

    A check is four things and `manage.py check` prints all four. A test that
    reads the id and a fragment of the message pins two of them and leaves the
    other two free -- and a hint that tells a developer to fix the wrong thing
    is worse than no hint at all, because they will follow it.
    """
    assert [problem.id for problem in problems] == [problem_id], [p.id for p in problems]
    problem = problems[0]
    assert problem.obj is obj, f"{problem_id} is about {problem.obj!r}, expected {obj!r}"
    assert problem.msg == message, f"{problem_id} says\n  {problem.msg}\nexpected\n  {message}"
    assert problem.hint == hint, f"{problem_id} hints\n  {problem.hint}\nexpected\n  {hint}"


# ------------------------------------------------------------- the declaration


def test_a_source_is_unpartitioned_by_default():
    """The default has to be None rather than anything cleverer. Every template
    branches on it, so a truthy default would rewrite every trigger body in
    every project that never asked for this."""
    assert SourceTable(schema="s", table="t").partition_key is None


def test_the_partition_key_survives_equality():
    """SourceTable is a frozen dataclass and is compared by value in a few
    places; a key that didn't participate would make two different sources
    compare equal."""
    assert SourceTable(schema="s", table="t", partition_key="state") != SourceTable(schema="s", table="t")


# ------------------------------------------------------------------ the FK side


def test_the_fk_carries_no_partition_column_by_default():
    assert Person._meta.get_field("id") is not None  # the model imported cleanly
    assert PersonAddressThrough._meta.get_field("person").partition_column is None


def test_the_partition_column_survives_a_deconstruct_round_trip():
    """The trigger body is written from migration state, so a field that lost
    this on the round trip would silently rebuild the unpruned probe the next
    time the migration replayed."""
    field = OverlayForeignKey(Person, on_delete=models.CASCADE, partition_column="state")
    _, _, _, kwargs = field.deconstruct()

    assert kwargs["partition_column"] == "state"


def test_an_absent_partition_column_stays_out_of_the_deconstruction():
    """Otherwise every existing migration would want rewriting to carry a None
    that means exactly what its absence already meant."""
    field = OverlayForeignKey(Person, on_delete=models.CASCADE)
    _, _, _, kwargs = field.deconstruct()

    assert "partition_column" not in kwargs


def test_the_target_tables_entry_needs_both_halves():
    """A source with no key has nothing to prune on; a key with no local column
    has no value to prune by. Either alone is not a usable declaration, and
    emitting half of one would put a dangling reference in the trigger."""
    with partitioned(Person, PARTITIONED):
        both = target_tables_for(Person, "public", partition_column="label")[1]
        key_only = target_tables_for(Person, "public")[1]
    with partitioned(Person, SourceTable(schema="public", table="testapp_shared_personsource")):
        column_only = target_tables_for(Person, "public", partition_column="label")[1]

    assert both["partition"] == {"column": "first_name", "local_column": "label"}
    assert key_only["partition"] is None
    assert column_only["partition"] is None


def test_the_field_hands_the_builder_its_own_partition_column():
    """target_tables_for() takes the column as an argument and the field is the
    only thing that knows it. This method is the entire route from a declared
    partition_column to a pruned predicate -- the migration path calls the
    function directly with state of its own."""
    field = PersonAddressThrough._meta.get_field("person")
    with partitioned(Person, PARTITIONED), mock.patch.object(field, "partition_column", "label"):
        source_entry = field.target_tables("public")[1]

    assert source_entry["partition"] == {"column": "first_name", "local_column": "label"}


def test_the_base_entry_never_carries_a_partition():
    """The target's base table is an ordinary unpartitioned table. A key
    predicate there would filter correctly and prune nothing, so the entry has
    no business carrying one."""
    with partitioned(Person, PARTITIONED):
        base = target_tables_for(Person, "public", partition_column="label")[0]

    assert "partition" not in base


# ----------------------------------------------------------------- the checks


def test_a_partition_key_the_model_has_no_field_for_is_an_error():
    """The view selects the key from both branches under one name, so a key the
    model cannot see is one no trigger can reference either. Caught at check
    time rather than as a runtime failure inside a trigger body."""
    with partitioned(Person, BAD_KEY):
        problems = _partition_key_problems(Person)

    assert_problem(
        problems,
        "django_overlay.E004",
        Person,
        "Person's source declares partition_key='not_a_column', but the model has no field with "
        "that column.",
        "The view selects 'not_a_column' from both branches under one name, so the model needs a "
        "field for it before any trigger can reference it. Add the field, or correct the "
        "partition_key.",
    )


def test_a_partition_key_matching_a_real_column_is_accepted():
    with partitioned(Person, PARTITIONED):
        assert _partition_key_problems(Person) == []


def test_an_unpartitioned_source_raises_nothing():
    assert _partition_key_problems(Person) == []


def test_an_fk_to_a_partitioned_target_without_a_partition_column_warns():
    """A warning and not an error: the probe is correct, just unpruned, and only
    the caller knows whether a referencing row shares its target's partition."""
    field = PersonAddressThrough._meta.get_field("person")
    with partitioned(Person, PARTITIONED):
        problems = _partition_column_problems(PersonAddressThrough, field)

    assert_problem(
        problems,
        "django_overlay.W003",
        field,
        "PersonAddressThrough.person points at Person, whose source is partitioned on "
        "'first_name', but no partition_column is declared. The FK's insert-side trigger will "
        "probe every partition on every write.",
        "If a PersonAddressThrough always shares its Person's 'first_name', pass "
        'partition_column="<that column>". If it genuinely references across partitions, leave '
        "this as it is — the probe is correct, just unpruned.",
    )


def test_an_fk_that_declares_its_partition_column_is_accepted():
    field = PersonAddressThrough._meta.get_field("person")
    with partitioned(Person, PARTITIONED), mock.patch.object(field, "partition_column", "label"):
        assert _partition_column_problems(PersonAddressThrough, field) == []


def test_a_partition_column_the_referencing_model_lacks_is_an_error():
    field = PersonAddressThrough._meta.get_field("person")
    with partitioned(Person, PARTITIONED), mock.patch.object(field, "partition_column", "not_a_column"):
        problems = _partition_column_problems(PersonAddressThrough, field)

    assert_problem(
        problems,
        "django_overlay.E005",
        field,
        "PersonAddressThrough.person declares partition_column='not_a_column', but "
        "PersonAddressThrough has no field with that column.",
        "It names a column on the referencing model that holds the target's partition key.",
    )


def test_a_foreign_key_to_a_model_that_is_not_an_overlay_is_not_asked_for_a_source():
    """`_is_overlay_view_model` is set by the metaclass, so a plain model does
    not carry the attribute at all -- and a plain model has no get_source() to
    call either. The default on that getattr is the whole of what stands
    between this check and an AttributeError, and it has to be falsy rather
    than merely present."""
    field = PersonAddressThrough._meta.get_field("person")
    with mock.patch.object(field.remote_field, "model", PersonProfile):
        assert _partition_column_problems(PersonAddressThrough, field) == []


def test_an_fk_to_an_unpartitioned_target_is_never_flagged():
    """The overwhelmingly common case, and the one that must stay silent."""
    field = AddressNote._meta.get_field("address")

    assert _partition_column_problems(AddressNote, field) == []
    assert Address.get_source().partition_key is None


def test_a_bad_partition_column_is_reported_even_at_an_unpartitioned_target():
    """It names a column on the referencing model, so it is wrong on its own
    terms whatever the target does -- and a target that gains a key later would
    otherwise turn a silent typo into a broken trigger."""
    field = AddressNote._meta.get_field("address")
    with mock.patch.object(field, "partition_column", "not_a_column"):
        problems = _partition_column_problems(AddressNote, field)

    assert [problem.id for problem in problems] == ["django_overlay.E005"]


# ------------------------------------------------------------- introspection


@pytest.mark.django_db
def test_an_ordinary_table_reports_no_partitions():
    """None, not an empty summary. The command branches on it to decide whether
    to say anything at all, and every table in this suite is ordinary."""
    with connection.cursor() as cursor:
        assert partition_summary(cursor, "public", "testapp_shared_personsource") is None


@pytest.mark.django_db
def test_a_table_that_does_not_exist_reports_no_partitions():
    with connection.cursor() as cursor:
        assert partition_summary(cursor, "public", "no_such_table_at_all") is None


# ------------------------------------------------- against a real partitioned table
#
# Everything above is about the declaration. This is the premise underneath it:
# that supplying the key is what turns an Append over every partition into a
# scan of one. Cheap to prove -- it is DDL and a plan, no rows needed -- and
# worth proving rather than assuming, because the entire feature is an
# optimisation that is invisible when it fails.


@pytest.fixture
def partitioned_table(db):
    """Three partitions, an index attached to the parent, and one index built
    directly on a single partition -- the half-covered case parity cannot see
    from the parent alone."""
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE probe_people (id bigint NOT NULL, state text NOT NULL, email text) PARTITION BY LIST (state)"
        )
        for state in ("ca", "tx", "ny"):
            cursor.execute(f"CREATE TABLE probe_people_{state} PARTITION OF probe_people FOR VALUES IN ('{state}')")
        cursor.execute("CREATE INDEX probe_people_id ON probe_people (id)")
        cursor.execute("CREATE INDEX probe_people_ca_email ON probe_people_ca (email)")
        cursor.execute("ANALYZE probe_people")
    yield
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE probe_people")


def scanned_partitions(predicate: str) -> int:
    """How many of the three partitions the plan actually reads."""
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN SELECT 1 FROM probe_people WHERE {predicate}")  # noqa: S608 - test literal
        plan = "\n".join(row[0] for row in cursor.fetchall())
    return sum(f"probe_people_{state}" in plan for state in ("ca", "tx", "ny"))


def test_a_probe_without_the_key_reads_every_partition(partitioned_table):
    """The shape every generated probe had before this feature: correct, and
    paying for all of them."""
    assert scanned_partitions("id = 42") == 3


def test_a_probe_carrying_the_key_reads_one(partitioned_table):
    """And the shape the templates emit now. This is the whole return on the
    declaration."""
    assert scanned_partitions("id = 42 AND state = 'ca'") == 1


def test_the_summary_counts_the_partitions(partitioned_table):
    """The count is the multiplier on every probe that cannot prune, which is
    why the command reports it even when nothing is wrong."""
    with connection.cursor() as cursor:
        summary = partition_summary(cursor, "public", "probe_people")

    assert summary["partitions"] == 3


def test_the_summary_finds_an_index_attached_to_nothing(partitioned_table):
    """The silent case. An index built on one partition is attached to no
    parent index, so `table_indexes()` reading the parent reports it as absent
    everywhere -- and parity would tell you to create a shape that already
    exists on part of the table."""
    with connection.cursor() as cursor:
        summary = partition_summary(cursor, "public", "probe_people")

    assert summary["unattached"] == [{"shape": "btree (email)", "on_partitions": 1}]


def test_an_index_created_on_the_parent_is_not_reported_as_unattached(partitioned_table):
    """The other half of the same claim: `CREATE INDEX` on the parent really
    does reach every partition, so it belongs in the ordinary index listing and
    must not be reported as a gap."""
    from django_overlay.introspection import table_indexes

    with connection.cursor() as cursor:
        summary = partition_summary(cursor, "public", "probe_people")
        shapes = {index["shape"] for index in table_indexes(cursor, "public", "probe_people")}

    assert "btree (id)" in shapes
    assert "btree (id)" not in {index["shape"] for index in summary["unattached"]}
