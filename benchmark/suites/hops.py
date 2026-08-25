"""Does the ban still hold at the depth a real saved search reaches?

The ban suite establishes two hops thoroughly: 27,637ms -> 405ms, two passes,
a 1.0x noise floor. It says nothing about three or four, and that is the depth
the application actually runs -- a saved search is a boolean tree of several
conditions, not a pair.

The distinction is not pedantry. The m2m fence worked at one hop and did not
compose to two: each fence handed the planner an InitPlan for its own subquery
and nothing supplied a joint selectivity, so two blind estimates multiplied
into 266,974,515,000. Assuming the ban composes because it works at two hops
would be the same error a second time.

So: sweep the hop count and watch the shape of the curve. Linear is fine.
Anything accelerating means the ban buys depth rather than fixing it, and the
useful number becomes how many hops it buys.

Both the plain resolve and the ordered page, because the LIMIT is what makes
the planner reach for a nested loop in the first place and the two thresholds
treat them differently.
"""

from django.test import override_settings

from benchmark import harness


NAME = "hops"
TITLE = "How the ban holds as a saved search gets deeper"

OFF = override_settings(DJANGO_OVERLAY_FORCE_HASH_JOINS=False)

# Ordered narrow-to-broad, so each added hop is a further restriction rather
# than a new way to multiply rows. The fourth is the tenant-owned plain table:
# a real saved search mixes vendor-sourced conditions with tenant-only ones,
# and by then the query has four relations regardless of what backs them.
HOPS = (
    ("addresses__city", "city0"),
    ("phones__kind", "mobile"),
    ("emails__domain", "example.com"),
    ("labels__kind", "volunteer"),
)

COLUMNS = ("hops", "ban off", "ban on", "gain", "plain", "rows")


def resolve(model, scope):
    return model.objects.filter(**scope).values("pk").distinct().count()


def page(model, scope):
    return len(list(model.objects.filter(**scope).order_by("id")[:200]))


def run(ctx):
    from tests.testapp.models import BenchPerson, PlainPerson

    for label, operation in (
        ("Resolve: how many people match (no LIMIT)", resolve),
        ("Ordered page: the first 200 by id (LIMIT)", page),
    ):
        section = harness.Section(label, COLUMNS)
        scope = {}
        for depth, (field, value) in enumerate(HOPS, start=1):
            scope = scope | {field: value}

            with OFF:
                unbanned, unbanned_value = ctx.measure(lambda s=scope, op=operation: op(BenchPerson, s), rounds=2)
            banned, banned_value = ctx.measure(lambda s=scope, op=operation: op(BenchPerson, s), rounds=2)
            plain, plain_value = ctx.measure(lambda s=scope, op=operation: op(PlainPerson, s), rounds=2)

            ctx.compare(
                f"{label} / {depth} hops (ban on vs off)", unbanned_value, banned_value, "the ban changed the result"
            )
            ctx.compare(f"{label} / {depth} hops (overlay vs plain)", banned_value, plain_value)

            section.add(
                ", ".join(scope),
                {"ban off": unbanned, "ban on": banned, "plain": plain},
                hops=str(depth),
                gain=harness.gain(unbanned, banned),
                rows="capped" if banned_value is None else f"{banned_value:,}",
            )
        yield section
