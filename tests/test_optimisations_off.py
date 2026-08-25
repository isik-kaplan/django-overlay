"""Every combination of the four query optimisations, checked for correctness.

Each is a performance mechanism that must not change an answer, and
each has an opt-out because each has a regime where it loses -- the array fence
crosses over from 2.0x faster to 2.3x slower somewhere between a 25,000-row and
a 1,000,000-row scope, and the nested-loop ban costs 5-13% on the two- and
three-hop shapes it is not aimed at. So the opt-outs are documented, and the
people who reach for one are by definition the people already in trouble.

Mutation testing cannot reach any of this. mutmut runs every mutant under the
default settings, so a non-default configuration is invisible to it by
construction -- and the ordinary suite pins the *optimised* behaviour, so it
cannot be run with a flag off either: 44 tests assert that the rewrite fired.

That left the combinations untested, and not for want of single-flag coverage:
every flag had an off-state test, but no test had ever turned two off at once,
which is exactly what `django-overlay benchmark --no-optimisations` runs. This
file closes that. Every combination, because the count is still small enough that choosing a
subset would need an argument nobody could check.

It is differential rather than SQL-pinning, deliberately. Turning an
optimisation off is *supposed* to change the SQL; what it must never change is
the rows. The failure this catches is the one that matters and the one an
assertion about SQL misses -- a configuration that returns different results, or
that does not run at all. The latter is not hypothetical: `OverlayFencedIn`
borrowed `OverlaySubqueryIn.as_sql`, whose zero-arg `super()` then resolved
outside its own MRO, and every m2m-fenced query raised TypeError with
DJANGO_OVERLAY_ARRAY_SUBQUERY_IN off. It took an A/B benchmark to find.
"""

import itertools

import pytest
from django.test import override_settings

from tests.testapp.models import (
    Member,
    Roster,
    RosterMembership,
    WideCustomer,
    WideOrder,
    WideProduct,
    WideRegion,
)
from tests.testapp_shared.models import (
    MemberSource,
    RosterMembershipSource,
    RosterSource,
    WideCustomerSource,
)


pytestmark = pytest.mark.django_db

SWITCHES = (
    "DJANGO_OVERLAY_REWRITE_TRAVERSALS",
    "DJANGO_OVERLAY_REDIRECT_SELECT_RELATED",
    "DJANGO_OVERLAY_FORCE_HASH_JOINS",
    "DJANGO_OVERLAY_ARRAY_SUBQUERY_IN",
    "DJANGO_OVERLAY_M2M_FENCE",
)

ALL_ON = dict.fromkeys(SWITCHES, True)


def _combinations():
    """Every on/off assignment, named by what is off."""
    for states in itertools.product((True, False), repeat=len(SWITCHES)):
        settings = dict(zip(SWITCHES, states, strict=True))
        off = [name for name, on in settings.items() if not on]
        label = (
            "everything-on"
            if not off
            else "-".join(name.removeprefix("DJANGO_OVERLAY_").lower().replace("_", "-") for name in off)
        )
        yield pytest.param(settings, id=label)


@pytest.fixture
def graph():
    """Source-backed and organic rows on both sides of both relations.

    The awkward parts earn their place: a null on the far side of a two-hop
    traversal is where a join and a semi-join stop agreeing, and a roster with
    two matching members is where a rewrite that deduplicated would show up --
    a join multiplies rows and a semi-join does not. WideOrder.customer is
    not nullable, so the null sits on WideCustomer.region instead.
    """
    region = WideRegion.objects.create(name="region7", country="GB")
    other = WideRegion.objects.create(name="region8", country="US")

    vendor_source = WideCustomerSource.objects.create(
        first_name="v",
        last_name="vendor",
        email="v@x",
        age=40,
        city="city42",
        postcode="pc1",
        status="active",
        score=10,
        registered_on="2020-01-01",
        notes="",
        region_id=region.id,
    )
    vendor = WideCustomer.objects.get(pk=-vendor_source.id)
    organic = WideCustomer.objects.create(
        first_name="o",
        last_name="organic",
        email="o@x",
        age=41,
        city="city42",
        postcode="pc2",
        status="lapsed",
        score=None,
        registered_on="2020-01-02",
        region=other,
    )
    # region=None: the far side of the two-hop traversal is null here, which is
    # where an inner join and a semi-join over the same predicate diverge.
    elsewhere = WideCustomer.objects.create(
        first_name="e",
        last_name="elsewhere",
        email="e@x",
        age=42,
        city="city99",
        postcode="pc3",
        status="active",
        score=30,
        registered_on="2020-01-03",
        region=None,
    )

    product = WideProduct.objects.create(
        sku="SKU1",
        name="p",
        category="cat7",
        price_cents=100,
        weight_grams=1,
        supplier="s",
    )
    for label, customer in (("vendor", vendor), ("organic", organic), ("elsewhere", elsewhere)):
        WideOrder.objects.create(
            reference=f"REF-{label}",
            status="new",
            total_cents=100,
            placed_on="2021-01-01",
            channel="web",
            currency="GBP",
            customer=customer,
        )
    # The m2m side: both ends and the through model are overlay views, so
    # `roster.members` is a three-view traversal. One membership is the vendor's
    # assertion and one is the tenant's, because the view is a UNION ALL of both
    # and a fence that only saw one half would still look right on a query that
    # matched the other.
    roster_source = RosterSource.objects.create(title="vendor-roster")
    roster = Roster.objects.get(pk=-roster_source.id)
    tenant_roster = Roster.objects.create(title="tenant-roster")

    member_source = MemberSource.objects.create(name="m")
    vendor_member = Member.objects.get(pk=-member_source.id)
    tenant_member = Member.objects.create(name="m")

    RosterMembershipSource.objects.create(
        roster_id=roster.pk,
        member_id=vendor_member.pk,
        role="member",
    )
    RosterMembership.objects.create(roster=roster, member=tenant_member)
    RosterMembership.objects.create(roster=tenant_roster, member=tenant_member)

    return {
        "vendor": vendor,
        "organic": organic,
        "elsewhere": elsewhere,
        "region": region,
        "product": product,
        "roster": roster,
    }


def _shapes(graph):
    """One entry per query shape, each a thunk returning a comparable answer.

    Thunks rather than querysets, because a queryset built under one
    configuration and evaluated under another would compile with whichever was
    active at iteration time and prove nothing.
    """
    both_cities = [graph["vendor"], graph["organic"]]
    return {
        "fk one hop": lambda: sorted(
            WideOrder.objects.filter(customer__city="city42").values_list("reference", flat=True)
        ),
        "fk two hops": lambda: sorted(
            WideOrder.objects.filter(customer__region__country="GB").values_list("reference", flat=True)
        ),
        "fk two hops, far side null": lambda: sorted(
            WideOrder.objects.filter(customer__region__isnull=True).values_list("reference", flat=True)
        ),
        "excluded traversal": lambda: sorted(
            WideOrder.objects.exclude(customer__city="city42").values_list("reference", flat=True)
        ),
        "ordered page": lambda: list(
            WideOrder.objects.filter(customer__city="city42")
            .order_by("reference")
            .values_list("reference", flat=True)[:2]
        ),
        "count over a traversal": lambda: WideOrder.objects.filter(customer__city="city42").count(),
        "subquery in": lambda: sorted(
            WideOrder.objects.filter(customer__in=WideCustomer.objects.filter(city="city42")).values_list(
                "reference", flat=True
            )
        ),
        # A literal list has no subquery to fence, so this is the branch the
        # array rewrite must decline -- and declining is what the crash was on.
        "literal list in": lambda: sorted(
            WideOrder.objects.filter(customer__in=both_cities).values_list("reference", flat=True)
        ),
        "select_related across the view": lambda: sorted(
            f"{order.reference}:{order.customer.city}" for order in WideOrder.objects.select_related("customer")
        ),
        # Multiplicity, not just membership: two matching members means the
        # roster appears twice, and a rewrite that turned the join into a
        # semi-join would silently return it once.
        "m2m forward, with multiplicity": lambda: sorted(
            Roster.objects.filter(members__name="m").values_list("title", flat=True)
        ),
        "m2m reverse": lambda: sorted(
            Member.objects.filter(rosters__title="vendor-roster").values_list("name", flat=True)
        ),
        "related manager": lambda: sorted(graph["roster"].members.all().values_list("name", flat=True)),
    }


@pytest.mark.parametrize("configuration", list(_combinations()))
def test_every_configuration_returns_the_same_rows(graph, configuration):
    """The answer is the contract; the plan is an implementation detail.

    Every shape is compared against the same shape with all four on, so a
    disagreement names the configuration and the shape rather than leaving a
    number to interpret.
    """
    shapes = _shapes(graph)

    with override_settings(**ALL_ON):
        expected = {name: thunk() for name, thunk in shapes.items()}

    disagreed, crashed = [], []
    with override_settings(**configuration):
        for name, thunk in shapes.items():
            try:
                actual = thunk()
            except Exception as error:  # noqa: BLE001 - the report is the point
                crashed.append(f"{name}: {type(error).__name__}: {error}")
                continue
            if actual != expected[name]:
                disagreed.append(f"{name}: {actual!r} != {expected[name]!r}")

    off = [name for name, on in configuration.items() if not on] or ["nothing"]
    assert not crashed, f"with {', '.join(off)} off, {len(crashed)} shape(s) could not run at all:\n  " + "\n  ".join(
        crashed
    )
    assert not disagreed, (
        f"with {', '.join(off)} off, {len(disagreed)} shape(s) returned different rows:\n  " + "\n  ".join(disagreed)
    )


def test_the_configurations_cover_every_switch_the_benchmark_knows_about():
    """The list here and the benchmark's table are two statements of one fact,
    and this file is worth nothing if it is testing three of four."""
    from benchmark import switches

    assert set(SWITCHES) == {switch.setting for switch in switches.SWITCHES}


def test_there_is_a_case_with_every_switch_off():
    """The arm `--no-optimisations` runs, and the one that had no test at all."""
    ids = {param.id for param in _combinations()}
    assert "everything-on" in ids
    everything_off = "-".join(name.removeprefix("DJANGO_OVERLAY_").lower().replace("_", "-") for name in SWITCHES)
    assert everything_off in ids
    assert len(ids) == 2 ** len(SWITCHES)
