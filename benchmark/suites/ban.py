"""What DJANGO_OVERLAY_FORCE_HASH_JOINS is worth, and what it costs.

The ban is on by default, so it answers for itself on both sides:

  * on the shapes it exists for -- two or more m2m hops, where the planner
    estimates 266,974,515,000 rows for a 132-row answer and picks a nested
    loop that runs to exhaustion;
  * on the shapes it deliberately stays out of. One m2m hop is already
    0.9x-2.3x a plain table, so if banning nested loops there is free, the
    threshold is over-cautious; if it is expensive, the threshold is load
    bearing. Either way the number should be written down rather than assumed,
    so the last section forces the ban below its own threshold to find out.

Three columns throughout: the ban off, the ban on, and a plain non-overlay
table holding identical rows. The plain column is the floor -- not something
the overlay can reach, but the thing that says whether a ratio is the view's
fault or the query's.
"""

from contextlib import contextmanager

from django.db.models import Count
from django.test import override_settings

from benchmark import harness
from django_overlay import models as overlay_models


NAME = "ban"
TITLE = "The nested-loop ban: what it is worth and what it costs"

OFF = override_settings(DJANGO_OVERLAY_FORCE_HASH_JOINS=False)

NARROW = {"addresses__city": "city0"}
BROAD = {"phones__kind": "mobile"}
SCALAR = {"city__in": [f"city{n}" for n in range(25)]}
PLAIN_HOP = {"labels__kind": "volunteer"}

COLUMNS = ("hops", "ban off", "ban on", "gain", "plain", "rows")


@contextmanager
def threshold(value):
    """Lower the hop threshold so the ban applies where it normally would not.

    Only for the last section, which measures what the ban costs on the shapes
    the threshold is there to protect.
    """
    previous = overlay_models._HASH_JOIN_THRESHOLD
    overlay_models._HASH_JOIN_THRESHOLD = value
    try:
        yield
    finally:
        overlay_models._HASH_JOIN_THRESHOLD = previous


def resolve(model, scope):
    return model.objects.filter(**scope).values("pk").distinct().count()


def page(model, scope):
    """An ordered page -- the shape where the LIMIT collapses the tuple
    fraction and the nested loop looks free."""
    return len(list(model.objects.filter(**scope).order_by("id")[:200]))


def summarise(model, scope):
    """The relation counts from a summary panel, over a joined scope."""
    return model.objects.filter(**scope).aggregate(
        total=Count("id", distinct=True),
        phones=Count("phones", distinct=True),
    )["total"]


def scoped_subquery(model, scope):
    """The scope attached as a subquery rather than filtered inline.

    The joins move out of the outer alias map and into the subquery, which is
    what made the first version of the detection miss this shape entirely.
    """
    return model.objects.filter(pk__in=model.objects.filter(**scope).values("pk")).count()


def leaf_by_leaf(model, scope):
    """Each condition resolved as its own subquery and chained.

    This shape already worked before the ban -- no two m2m joins ever share a
    plan -- so the question here is not whether the ban rescues it but whether
    it leaves it alone. Now that subqueries are counted, it trips the
    threshold, and a rescue that slows down the thing that already worked is
    not a rescue.
    """
    queryset = model.objects.all()
    for field, value in scope.items():
        queryset = queryset.filter(pk__in=model.objects.filter(**{field: value}).values("pk"))
    return queryset.values("pk").distinct().count()


def assert_setting_is_clean(ctx):
    """Every cell starts from the same session state.

    If a restore ever failed, later "ban off" cells would silently run banned
    and the comparison would quietly become meaningless rather than wrong in a
    visible way.
    """
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("SHOW enable_nestloop")
        if cursor.fetchone()[0] != "on":
            ctx.disagreements.append("a previous cell leaked the nested-loop ban")


def row(ctx, section, label, operation, scope, hops, models):
    overlay_model, plain_model = models
    assert_setting_is_clean(ctx)

    with OFF:
        unbanned, unbanned_value = ctx.measure(lambda: operation(overlay_model, scope))
    banned, banned_value = ctx.measure(lambda: operation(overlay_model, scope))
    plain, plain_value = ctx.measure(lambda: operation(plain_model, scope))

    ctx.compare(f"{label} (ban on vs off)", unbanned_value, banned_value,
                "the ban changed the result")
    ctx.compare(f"{label} (overlay vs plain)", banned_value, plain_value)

    section.add(
        label,
        {"ban off": unbanned, "ban on": banned, "plain": plain},
        hops=str(hops),
        gain=harness.gain(unbanned, banned),
        rows="capped" if banned_value is None else f"{banned_value:,}",
    )


def run(ctx):
    from tests.testapp.models import BenchPerson, PlainPerson

    models = (BenchPerson, PlainPerson)

    # Twice by default, because the first version of this probe contradicted
    # its own previous run and there was no way to tell signal from drift.
    # Anything that moves between passes is drift; the control section says how
    # much drift to expect.
    for attempt in range(1, ctx.passes + 1):
        suffix = f" (pass {attempt})" if ctx.passes > 1 else ""

        section = harness.Section(f"Two or more hops -- what the ban is for{suffix}", COLUMNS)
        row(ctx, section, "two hops, narrow + broad", resolve, NARROW | BROAD, 2, models)
        row(ctx, section, "two hops + a scalar scope", resolve, NARROW | BROAD | SCALAR, 2, models)
        row(ctx, section, "two hops, ordered page", page, NARROW | BROAD, 2, models)
        row(ctx, section, "two hops, summary counts", summarise, NARROW | BROAD, 2, models)
        row(ctx, section, "two hops, scope as subquery", scoped_subquery, NARROW | BROAD, 2, models)
        row(ctx, section, "two leaves, chained subqueries", leaf_by_leaf, NARROW | BROAD, 2, models)
        yield section

        section = harness.Section(
            f"One hop -- banned only when there is a LIMIT{suffix}", COLUMNS,
            note="the limited threshold is 2, the unlimited one 4",
        )
        row(ctx, section, "one hop, narrow", resolve, NARROW, 1, models)
        row(ctx, section, "one hop, broad", resolve, BROAD, 1, models)
        row(ctx, section, "one hop, ordered page", page, BROAD, 1, models)
        yield section

        # Neither column applies the ban, so the two should agree. Whatever gap
        # shows up here is what the harness cannot measure below, and no gain
        # in the tables above is meaningful unless it clears it.
        section = harness.Section(f"Never banned -- the noise floor{suffix}", COLUMNS)
        row(ctx, section, "view -> plain table", resolve, PLAIN_HOP, 1, models)
        row(ctx, section, "no join at all", resolve, SCALAR, 0, models)
        yield section

    section = harness.Section(
        "What the exclusions would cost if banned anyway (threshold forced to 1)", COLUMNS,
        note="at 1,000,000 rows banning these was free, which argued for one threshold; "
             "the hash the ban forces is built over a larger relation as scale grows",
    )
    with threshold(1):
        row(ctx, section, "one hop, narrow", resolve, NARROW, 1, models)
        row(ctx, section, "one hop, broad", resolve, BROAD, 1, models)
        row(ctx, section, "view -> plain table", resolve, PLAIN_HOP, 1, models)
        # No user-written join at all -- but the view itself contains one, the
        # NOT EXISTS anti-join against the base table, so there is still a plan
        # here for the ban to change.
        row(ctx, section, "no join at all", resolve, SCALAR, 0, models)
    yield section
