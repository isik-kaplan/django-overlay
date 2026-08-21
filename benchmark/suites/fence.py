"""What the m2m fence is worth, swept from a narrow scope to a broad one.

The fence adds a predicate that an existing join already implies -- `person.id
= ANY (ARRAY(SELECT ...))` alongside the join it is redundant with -- so the
rows cannot change and only the plan can. It is on by default, so it answers
for itself on both sides.

The reason to sweep rather than take a point is that this one is known to
cross over. `ARRAY(subquery)` is an InitPlan: evaluated once, materialised, and
handed to the outer plan as a constant, which is exactly why the outer plan
stops having to estimate a relation it has no statistics for. The price is the
materialisation, roughly 16 bytes per row for a uuid key, paid on every
execution. A selective scope pays almost nothing for a plan that cannot go
wrong; a scope matching most of the table pays for the whole table.

The number in OverlayFencedIn's docstring -- 2.0x faster at a 25,000-row scope
and 2.3x slower at a 1,000,000-row one -- came from a probe that has since been
deleted, so it is currently unreproducible. This is what replaces it, and what
any change to the default has to argue against.

Nothing here makes the library adapt: the fence is decided by query shape, and
where the shape is not enough the answer is this measurement plus a manual
`filter(pk__overlay_fenced_in=...)`, never a runtime probe. See
docs/development/BENCHMARKS.md.
"""

from django.test import override_settings

from benchmark import harness


NAME = "fence"
TITLE = "The m2m fence: where materialising the scope stops paying"

OFF = override_settings(DJANGO_OVERLAY_M2M_FENCE=False)

# Narrow to broad, by how many rows the fenced subquery returns. `city` carries
# 1,000 distinct values, so each step multiplies the scope by roughly ten and
# the last one is every row with an address at all -- the case the fence is
# documented as losing on.
SCOPES = (
    ("1 city", {"addresses__city": "city0"}),
    ("10 cities", {"addresses__city__in": [f"city{n}" for n in range(10)]}),
    ("100 cities", {"addresses__city__in": [f"city{n}" for n in range(100)]}),
    ("every city", {"addresses__city__isnull": False}),
)

COLUMNS = ("fence off", "fence on", "gain", "plain", "rows")

# The hand-written section compares two compiled forms rather than a setting, so
# its columns say which form rather than on and off.
FORM_COLUMNS = ("plain IN", "ARRAY", "gain", "plain", "rows")


def resolve(model, scope):
    """The distinct people a saved search matches."""
    return model.objects.filter(**scope).values("pk").distinct().count()


def page(model, scope):
    """An ordered page over the same scope."""
    return len(list(model.objects.filter(**scope).order_by("id")[:200]))


def scoped(model, scope, fenced):
    """The scope as a subquery, fenced by hand or left as a plain `IN`.

    This is the array rewrite on its own, with nothing else varying: one
    subquery, compiled two ways. `pk__overlay_fenced_in` is the supported way to
    ask for it, and what somebody reaches for when the sweep says their scope is
    on the paying side of the crossover.
    """
    inner = model.objects.filter(**scope).values("pk")
    fenceable = fenced and getattr(model, "_is_overlay_view_model", False)
    lookup = "pk__overlay_fenced_in" if fenceable else "pk__in"
    return model.objects.filter(**{lookup: inner}).values("pk").distinct().count()


def row(ctx, section, label, operation, scope, models):
    overlay_model, plain_model = models

    with OFF:
        unfenced, unfenced_value = ctx.measure(lambda: operation(overlay_model, scope))
    fenced, fenced_value = ctx.measure(lambda: operation(overlay_model, scope))
    plain, plain_value = ctx.measure(lambda: operation(plain_model, scope))

    # The whole argument for the fence is that it is implied by a conjunct
    # already present, so a difference here is the finding, not the timing.
    ctx.compare(f"{label} (fence on vs off)", unfenced_value, fenced_value,
                "the fence changed the result")
    ctx.compare(f"{label} (overlay vs plain)", fenced_value, plain_value)

    section.add(
        label,
        {"fence off": unfenced, "fence on": fenced, "plain": plain},
        gain=harness.gain(unfenced, fenced),
        rows="capped" if fenced_value is None else f"{fenced_value:,}",
    )


def form_row(ctx, section, label, scope, models):
    """One scope, compiled as a plain `IN` and as an `ARRAY`.

    The automatic fence stays *on* for both columns, which is both the honest
    comparison and the only runnable one.

    Honest, because it is the question somebody actually has: with defaults on,
    is it worth hand-writing the fence on an outer `pk__in`? The inner
    traversal is fenced either way -- `_m2m_fence` does not touch `pk__in`,
    since `pk` is not a relation -- so the only difference between the columns
    is the outer lookup's compiled form.

    Only runnable, because turning the fence off here does not produce a slow
    measurement, it produces a crashed server. The unfenced inner traversal
    plans a Parallel Hash over 7,950,000,000 estimated rows for a 200-row
    answer, that node's tuplestore lives in dynamic shared memory, and the
    backend is OOM-killed. The first version of this section disabled the fence
    and took Postgres down on its first row at scale 1.0.
    """
    overlay_model, plain_model = models

    plain_in, plain_in_value = ctx.measure(lambda: scoped(overlay_model, scope, False))
    arrayed, arrayed_value = ctx.measure(lambda: scoped(overlay_model, scope, True))
    floor, floor_value = ctx.measure(lambda: scoped(plain_model, scope, False))

    ctx.compare(f"{label} (ARRAY vs plain IN)", plain_in_value, arrayed_value,
                "the array form changed the result")
    ctx.compare(f"{label} (overlay vs plain)", arrayed_value, floor_value)

    section.add(
        label,
        {"plain IN": plain_in, "ARRAY": arrayed, "plain": floor},
        gain=harness.gain(plain_in, arrayed),
        rows="capped" if arrayed_value is None else f"{arrayed_value:,}",
    )


def run(ctx):
    from tests.testapp.models import BenchPerson, PlainPerson

    models = (BenchPerson, PlainPerson)

    for attempt in range(1, ctx.passes + 1):
        suffix = f" (pass {attempt})" if ctx.passes > 1 else ""

        section = harness.Section(
            f"Resolve a scope, narrow to broad{suffix}", COLUMNS,
            note="the fence materialises the scope, so the cost grows with it",
        )
        for label, scope in SCOPES:
            row(ctx, section, label, resolve, scope, models)
        yield section

        section = harness.Section(
            f"The same scopes, as an ordered page{suffix}", COLUMNS,
            note="a LIMIT changes which plan the estimate picks, so it is swept too",
        )
        for label, scope in SCOPES:
            row(ctx, section, label, page, scope, models)
        yield section

        section = harness.Section(
            f"The array rewrite alone: one subquery, two compiled forms{suffix}",
            FORM_COLUMNS,
            note="pk__in against pk__overlay_fenced_in, automatic fence on in both",
        )
        for label, scope in SCOPES:
            form_row(ctx, section, label, scope, models)
        yield section
