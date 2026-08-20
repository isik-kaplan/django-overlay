"""The guards on the m2m fence, one test each.

`_m2m_fence` adds a conjunct to an m2m traversal — `pk = ANY (ARRAY(SELECT ...))`
— which is implied by the join Django already emitted, so it cannot change which
rows match, only what the planner knows. See the docstring on the method for the
measurements.

Everything here is about when it must *not* fire. Each early return in that
method is a separate reason, and every one of them had surviving mutants: the
conditions could be inverted, their operators swapped, their arguments dropped,
and no test noticed, because the only assertions nearby were about the case
where the fence does fire.

The fence is visible in the compiled SQL, which is what these assert on. That is
deliberate: it is a planner hint, so its presence or absence *is* the behaviour.
"""

from unittest import mock

import pytest
from django.db.models import F

from django_overlay.models import _fence_suppressed
from tests.testapp.models import Member, Person, Roster, RosterMembership, WideOrder


pytestmark = pytest.mark.django_db


def where(queryset) -> str:
    sql = str(queryset.query)
    return sql[sql.index(" WHERE ") :] if " WHERE " in sql else ""


def test_build_filter_forwards_what_it_was_given_down_every_branch():
    """build_filter overrides Django's and calls it back three times over --
    once for a rewritten expression, once for the original, and once for the
    fence -- and each call site forwards separately.

    Django passes its own arguments by keyword, so `*args` is empty in
    practice and dropping either half of the forwarding changed nothing the
    suite could see: five mutants lived across those three call sites. The
    contract of a transparent wrapper is that it is transparent, which is what
    is asserted here, positionally and by keyword.
    """
    from django.db.models.sql.query import Query as DjangoQuery

    real = DjangoQuery.build_filter
    calls = []

    def recorder(self, filter_expr, *args, **kwargs):
        calls.append((args, dict(kwargs)))
        return real(self, filter_expr, *args, **kwargs)

    def forwarded(query, filter_expr):
        """How many of the calls carried everything through untouched.

        Django's own build_filter recurses -- resolving a subquery reaches it
        again -- and it passes its arguments by keyword, so those inner calls
        arrive with no positional arguments at all. Counting the ones that
        match distinguishes the forwarding under test from Django's own.
        """
        calls.clear()
        # Identity, not equality, for can_reuse: Django adds the aliases it
        # used to that very set, so a set compared after the call is no longer
        # the empty one that went in.
        reuse = set()
        with mock.patch.object(DjangoQuery, "build_filter", recorder):
            query.build_filter(filter_expr, False, False, can_reuse=reuse, check_filterable=False)
        assert calls, "super().build_filter was never reached"
        return [
            (args, kwargs)
            for args, kwargs in calls
            if args == (False, False)
            and kwargs.get("can_reuse") is reuse
            and kwargs.get("check_filterable") is False
        ]

    # The rewritten branch: a cross-view traversal becomes `fk__in=<subquery>`.
    rewritten = forwarded(WideOrder.objects.all().query, ("customer__city", "city42"))
    assert len(rewritten) == 1

    # The plain and fenced branches, both reached from the one call.
    fenced = forwarded(Roster.objects.all().query, ("members__name", "m"))
    assert len(fenced) == 2, "the original expression and the fence are forwarded separately"


def test_a_plain_m2m_traversal_is_fenced():
    """The precondition for every other test here: without this the absence
    assertions below would pass for a fence that never works at all."""
    assert "= ANY (ARRAY(SELECT" in where(Roster.objects.filter(members__name="m"))


def test_a_negated_traversal_is_not_fenced():
    assert "= ANY (ARRAY(SELECT" not in where(Roster.objects.exclude(members__name="m"))


@pytest.mark.parametrize(
    "branch_negated, current_negated",
    [(True, True), (True, False), (False, True)],
)
def test_the_fence_refuses_under_any_negation(branch_negated, current_negated):
    """NOT (A AND B) is not NOT A, so either flag alone has to stop it.

    Asserted on the method rather than on the SQL, and that is the point. An
    `exclude()` sets both flags together, so `or` and `and` between them agree
    and a mutation of the operator is invisible; the shape that sets exactly one
    -- `exclude(~Q(...))` -- also makes other, unnegated calls that do add a
    fence, so the compiled SQL contains one either way. The method's own answer
    is the only clean observation.
    """
    query = Roster.objects.filter(members__name="m").query

    assert query._m2m_fence(
        ("members__name", "m"),
        branch_negated=branch_negated,
        current_negated=current_negated,
    ) is None


def test_the_fence_is_built_when_nothing_is_negated():
    """The control. Without it every assertion above holds for a method that
    returns None unconditionally."""
    query = Roster.objects.filter(members__name="m").query

    fence = query._m2m_fence(
        ("members__name", "m"), branch_negated=False, current_negated=False
    )

    assert fence is not None
    path, value = fence
    assert path == "pk__overlay_fenced_in"


def test_an_expression_value_is_not_fenced():
    """The fence puts the value in an ARRAY subquery, which needs a value, not
    something that resolves against the row."""
    queryset = Roster.objects.filter(members__name=F("title"))

    assert "= ANY (ARRAY(SELECT" not in where(queryset)


def test_a_path_that_is_not_a_string_is_refused_without_touching_it():
    """The two halves of that guard are `or`-ed for a reason.

    `"__" not in path` on a non-string raises, so the isinstance check has to
    short-circuit it. With `and` in between, the second half is evaluated and
    the method blows up on input it is supposed to decline.
    """
    query = Roster.objects.filter(members__name="m").query

    assert query._m2m_fence((5, "m"), branch_negated=False, current_negated=False) is None


def test_a_nested_lookup_is_still_fenced():
    """`partition` takes the first `__`, not the last.

    With `rpartition` the head becomes "members__name" -- not a field -- and
    the fence silently stops being built for every lookup with a modifier on
    it, which is most of them.
    """
    assert "= ANY (ARRAY(SELECT" in where(Roster.objects.filter(members__name__startswith="m"))


def test_an_m2m_through_a_plain_model_is_not_fenced():
    """Person.phones goes through a plain table, so only one view is involved.

    The guards read `_is_overlay_view_model` off the through and target models
    with a default of False, and a plain model does not have the attribute at
    all -- so the default is what answers. Defaulting to True instead fences a
    traversal that has nothing to fence, and dropping the default turns a
    missing attribute into an AttributeError.
    """
    assert "= ANY (ARRAY(SELECT" not in where(Person.objects.filter(phones__number="555"))


def test_the_fence_reads_the_through_and_target_views():
    """It is the through view the subquery walks, and that is what makes it
    implied by the join Django already emitted."""
    clause = where(Roster.objects.filter(members__name="m"))

    assert "rostermembership_view" in clause
    assert "member_view" in clause


def test_the_fences_own_subquery_is_fenced_too():
    """The inner queryset is built on an OverlayQuery, not a plain one.

    Given a plain Query the inner `member__name` filter compiles to an INNER
    JOIN against the member view instead of nesting a second fence -- the exact
    join shape the fence exists to avoid, reintroduced one level down. Two
    mutants dropped that argument and nothing noticed, because the outer fence
    was still there to satisfy every assertion about it.
    """
    clause = where(Roster.objects.filter(members__name="m"))
    fence = clause[clause.index("ANY (ARRAY(") :]

    assert "INNER JOIN" not in fence
    assert fence.count("ANY (ARRAY(") == 2, "the inner traversal should be fenced in turn"


def test_the_target_side_guard_answers_when_the_attribute_is_missing():
    """The through check short-circuits, so the target check needs its own case.

    Person.phones exercises the through guard but never reaches this one. There
    is no fixture with an overlay through and a plain target, so the attribute
    is removed from the target instead -- which is what the default exists for,
    and defaulting to True fences a traversal it must not.
    """
    absent = object()
    saved = Member.__dict__.get("_is_overlay_view_model", absent)
    del Member._is_overlay_view_model
    try:
        assert "= ANY (ARRAY(SELECT" not in where(Roster.objects.filter(members__name="m"))
    finally:
        if saved is not absent:
            Member._is_overlay_view_model = saved


def test_a_traversal_with_no_double_underscore_is_not_fenced():
    """`filter(phones=...)` is a single hop with nothing to fence past."""
    assert "= ANY (ARRAY(SELECT" not in where(Roster.objects.filter(title="t"))


def test_suppressing_the_fence_turns_it_off():
    """The suppression flag exists so the benchmark can measure both sides."""
    token = _fence_suppressed.set(True)
    try:
        assert "= ANY (ARRAY(SELECT" not in where(Roster.objects.filter(members__name="m"))
    finally:
        _fence_suppressed.reset(token)


@pytest.fixture
def roster_with_two_matching_members():
    """Two members with the same name, so the join multiplies the roster."""
    roster = Roster.objects.create(title="r")
    for _ in range(2):
        RosterMembership.objects.create(roster=roster, member=Member.objects.create(name="m"))
    return roster


def test_the_fence_does_not_change_the_rows(roster_with_two_matching_members):
    """The whole argument for adding it: implied by an existing conjunct, so
    the answer and the multiplicity are identical either way."""
    fenced = list(Roster.objects.filter(members__name="m").values_list("pk", flat=True))

    token = _fence_suppressed.set(True)
    try:
        plain = list(Roster.objects.filter(members__name="m").values_list("pk", flat=True))
    finally:
        _fence_suppressed.reset(token)

    assert sorted(fenced) == sorted(plain)
    assert len(fenced) == 2, "a roster with two matching members must appear twice"


def test_the_lookups_prefetch_emits_are_fenceable(roster_with_two_matching_members):
    """`in` and `exact` are named because prefetch_related() emits them.

    They are the tail *immediately* after the relation -- `members__in=[...]`,
    not `members__name__in=[...]`, whose tail is a field path and is fenceable
    for a different reason. Nothing in the suite filtered that way, so both
    spellings could be garbled without any effect.
    """
    member = Member.objects.filter(name="m").first()

    assert "= ANY (ARRAY(SELECT" in where(Roster.objects.filter(members__in=[member]))
    assert "= ANY (ARRAY(SELECT" in where(Roster.objects.filter(members__exact=member))


def test_a_tail_that_names_no_field_is_not_fenced():
    """The control for the set above.

    `members__name__isnull` is fenced -- its head is a real field, so there is
    still a join to fence, whatever the lookup on it. A tail whose head names
    nothing on the far model is what must be declined, and Django accepts one
    such spelling: a transform registered on the queryset's own lookups is out
    of scope here, so the plain case is a bare `pk` alias versus a name that is
    not a field at all.
    """
    assert "= ANY (ARRAY(SELECT" in where(Roster.objects.filter(members__pk__in=[1]))

    query = Roster.objects.filter(members__name="m").query
    assert query._is_fenceable_tail(Member, "name") is True
    assert query._is_fenceable_tail(Member, "not_a_field") is False
    assert query._is_fenceable_tail(Member, "not_a_field__in") is False


def test_split_exclude_forwards_its_keyword_arguments():
    """OverlayQuery overrides split_exclude to suppress the fence inside it.

    Django calls it positionally, so dropping **kwargs on the way to super()
    changed nothing observable -- but this overrides an API whose signature the
    library does not control, and the keyword form has to keep working.
    """
    from unittest import mock

    from django.db.models.sql.query import Query as DjangoQuery

    query = Roster.objects.filter(members__name="m").query.clone()

    with mock.patch.object(DjangoQuery, "split_exclude", return_value="delegated") as base:
        result = query.split_exclude(
            ("members__name", "m"), can_reuse=set(), names_with_path=[]
        )

    assert result == "delegated"
    assert base.call_args.kwargs == {"can_reuse": set(), "names_with_path": []}
