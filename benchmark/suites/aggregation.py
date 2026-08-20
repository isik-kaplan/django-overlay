"""A summary panel over a whole list: totals, buckets, and reach counts.

The shape is one statement over a filtered list of people -- a total, a dozen
or so bucket counts over person columns, and "how many distinct phones /
emails / addresses does this list reach". No LIMIT, no ordering, hundreds of
thousands of people in scope.

This is a different shape from every other suite here. The rest measure point
lookups and ordered pages, where the overlay's cost is the planner
mis-estimating a join under a collapsed tuple fraction. There is no LIMIT here
to collapse it and no narrow filter to plan around: the query reads the whole
scope and folds it. So the question is whether the overlay's per-row cost --
the UNION ALL and the anti-join above it -- is even visible once the work is
dominated by aggregation.

The normalisation matters more than the overlay does, and that is the point of
measuring five ways of writing the same summary:

  A. one .aggregate() with everything in it.
     Each relation count adds a join, the joins multiply, and so every
     person-column bucket must become Count(distinct=True) to survive that.
     Postgres cannot hash several distinct aggregates in one pass -- it runs a
     separate sort per aggregate. Twenty aggregates is twenty sorts.

  B. the person-column buckets in one query with no joins at all, and each
     relation count as its own query. Four statements, no distinct anywhere
     except the three relation counts, which genuinely need it.

  C-E. B, but with the scope resolved once into an id set the four statements
     reuse, instead of each one re-running the scope filter. This is the shape
     a "resolve the list, then summarise it" implementation has, and the one
     that matters when the scope is itself join-heavy. The three differ in how
     the subquery is attached.
"""

from datetime import date

from django.core.exceptions import FieldError
from django.db.models import Count, Q
from django.db.models.constants import LOOKUP_SEP

from benchmark import harness


NAME = "aggregation"
TITLE = "A summary panel over a whole list, written five ways"

# Scopes, smallest first. A saved search over a person column is the cheap
# case; the last one is join-heavy, which is where resolving the scope once
# can pay for itself.
SCOPES = (
    ("2.5% of people (25 cities)", {"city__in": [f"city{n}" for n in range(25)]}),
    ("50% of people (score < 500)", {"score__lt": 500}),
    ("everyone", {}),
    ("a join-heavy saved search", {"addresses__country": "US", "phones__kind": "mobile"}),
)

# Sixteen buckets over person columns, plus a total and three relation counts:
# twenty aggregates, which is the size a real summary panel runs to.
SCORE_BUCKETS = {
    "score_000_199": Q(score__lt=200),
    "score_200_399": Q(score__gte=200, score__lt=400),
    "score_400_599": Q(score__gte=400, score__lt=600),
    "score_600_799": Q(score__gte=600, score__lt=800),
    "score_800_plus": Q(score__gte=800),
}
STATUS_BUCKETS = {
    f"status_{status}": Q(status=status)
    for status in ("active", "lapsed", "pending", "closed")
}
DECADE_BUCKETS = {
    f"born_{decade}s": Q(born_on__gte=date(decade, 1, 1), born_on__lt=date(decade + 10, 1, 1))
    for decade in (1950, 1960, 1970, 1980, 1990, 2000, 2010)
}
BUCKETS = {**SCORE_BUCKETS, **STATUS_BUCKETS, **DECADE_BUCKETS}

# "How many distinct phones does this list reach", one per relation. These are
# the only aggregates that genuinely need DISTINCT -- the join multiplies.
RELATIONS = {"phone_count": "phones", "email_count": "emails", "address_count": "addresses"}

COLUMNS = ("overlay", "plain", "ratio", "people", "notes")


def buckets_for(distinct):
    """The person-column aggregates.

    `distinct` is not a style choice. With a relation join in the same query
    every person appears once per (address, phone, email) combination, so a
    plain Count over-counts by the multiplication factor. Shape A has to pay
    for it; the others avoid the join instead, which is the cheaper way to get
    the same number.
    """
    aggregates = {"total": Count("id", distinct=distinct)}
    for name, condition in BUCKETS.items():
        aggregates[name] = Count("id", distinct=distinct, filter=condition)
    return aggregates


def scope_joins(scope):
    """Does this scope traverse a relation, and so multiply rows?

    `city__in` does not; `addresses__country` does. Anything with a `__` before
    a lookup name is a path through a relation -- and every scope in SCOPES
    that uses one is a relation path, so testing for the separator is enough
    here. A general implementation would have to walk `_meta`.
    """
    return any(LOOKUP_SEP in key for key in scope)


def shape_a(model, scope):
    """Everything in one .aggregate(): twenty aggregates over a multiplied join."""
    return model.objects.filter(**scope).aggregate(
        **buckets_for(distinct=True),
        **{alias: Count(path, distinct=True) for alias, path in RELATIONS.items()},
    )


def shape_b(model, scope):
    """Buckets in one query, then one query per relation.

    The buckets need DISTINCT exactly when the *scope* joins. Hardcoding
    `distinct=False` here was wrong: it is right for a scope like `city__in`,
    which touches only the person table, and silently wrong for one like
    `addresses__country`, whose own join multiplies every person by their
    matching addresses. Measured at 1,000,000 people, that reported 206,664
    people where there were 59,999, with 14 of the 20 aggregates inflated --
    on plain tables as much as on the overlay, since it is Django's join
    semantics rather than anything about the view.
    """
    distinct = scope_joins(scope)
    summary = model.objects.filter(**scope).aggregate(**buckets_for(distinct=distinct))
    for alias, path in RELATIONS.items():
        summary |= model.objects.filter(**scope).aggregate(**{alias: Count(path, distinct=True)})
    return summary


def _resolved(model, scope, lookup, distinct):
    """The four statements of shape B, scoped by a subquery instead of by
    repeating the filter.

    A subquery rather than a materialised list of ids: half a million keys
    round-tripped into Python and back out as query parameters is its own
    bottleneck, and it is not what an ORM-only implementation would write.

    `lookup` picks how the subquery is attached, which is the whole experiment:

      "in"                -- what anyone would write. Plain Django. Note that
                             DJANGO_OVERLAY_ARRAY_SUBQUERY_IN does *not* reach
                             here: OverlaySubqueryIn is registered on
                             OverlayForeignKey, and this is a primary key.
      "overlay_fenced_in" -- the same `= ANY (ARRAY(...))` rewrite the m2m fence
                             uses, resolved by OverlayQuery.build_lookup().

    `distinct` is the other axis. Without it the subquery returns one row per
    joined combination, so a join-heavy scope hands the outer query several
    times more keys than there are people. `IN` dedups anyway, but `ARRAY()`
    materialises every duplicate.
    """
    inner = model.objects.filter(**scope).values("pk")
    if distinct:
        inner = inner.distinct()
    scoped = {f"pk__{lookup}": inner}
    summary = model.objects.filter(**scoped).aggregate(**buckets_for(distinct=False))
    for alias, path in RELATIONS.items():
        summary |= model.objects.filter(**scoped).aggregate(**{alias: Count(path, distinct=True)})
    return summary


def shape_c(model, scope):
    """Scope resolved once, attached with a plain `pk__in`."""
    return _resolved(model, scope, lookup="in", distinct=False)


def shape_d(model, scope):
    """Shape C plus `.distinct()` on the subquery -- does deduping alone do it?"""
    return _resolved(model, scope, lookup="in", distinct=True)


def shape_e(model, scope):
    """Shape D plus the array rewrite -- does `= ANY (ARRAY(...))` add anything?

    Overlay-only: `overlay_fenced_in` is resolved by OverlayQuery.build_lookup()
    and registered on no field, so a plain model cannot reach it at all. There
    is no plain baseline to print for this row, which is itself the honest
    reading -- an ordinary Django app has no way to write this.
    """
    return _resolved(model, scope, lookup="overlay_fenced_in", distinct=True)


SHAPES = (
    ("A  one aggregate()", shape_a),
    ("B  split per relation", shape_b),
    ("C  scope: pk__in", shape_c),
    ("D  scope: pk__in distinct", shape_d),
    ("E  scope: fenced array", shape_e),
)
OVERLAY_ONLY = {"E  scope: fenced array"}


def _which_form_compiles_to_an_array(models):
    """Printed rather than assumed.

    `pk__in` reads like it should pick up DJANGO_OVERLAY_ARRAY_SUBQUERY_IN and
    does not, because that lookup is registered on OverlayForeignKey and a
    primary key is not one. The plain model is included to show that
    `overlay_fenced_in` is unreachable there.
    """
    section = harness.Section(
        "Which scoping form compiles to `= ANY (ARRAY(...))`?", ("form", "compiles to"),
    )
    for model in models:
        for lookup in ("in", "overlay_fenced_in"):
            inner = model.objects.filter(city="city42").values("pk")
            try:
                statement, _ = model.objects.filter(
                    **{f"pk__{lookup}": inner}
                ).query.sql_with_params()
            except FieldError as error:
                verdict = f"unreachable ({type(error).__name__})"
            else:
                verdict = ("ARRAY initplan" if "= ANY (ARRAY" in statement
                           else "plain IN semi-join")
            section.add(model.__name__, form=f"pk__{lookup}", **{"compiles to": verdict})
    return section


def run(ctx):
    from tests.testapp.models import BenchPerson, PlainPerson

    yield _which_form_compiles_to_an_array((BenchPerson, PlainPerson))

    for scope_label, scope in SCOPES:
        section = harness.Section(f"Scope: {scope_label}", COLUMNS)

        # Every shape must agree with every other, on both models, or the
        # numbers are timings of five different questions. Each model is
        # checked against its own earlier shapes, independently: pairing them
        # would throw away a good result whenever the other side capped --
        # which is exactly when a shape is most likely to be answering a
        # different question, since the thing that makes it slow (a join that
        # multiplies) is also the thing that corrupts its counts.
        agreed = {}
        for shape_label, shape in SHAPES:
            overlay_only = shape_label in OVERLAY_ONLY
            overlay, overlay_result = ctx.measure(
                lambda f=shape, s=scope: f(BenchPerson, s), rounds=2)
            if overlay_only:
                plain, plain_result = None, None
            else:
                plain, plain_result = ctx.measure(
                    lambda f=shape, s=scope: f(PlainPerson, s), rounds=2)

            notes = []
            sides = [("overlay", overlay_result)]
            if overlay_only:
                notes.append("overlay-only")
            else:
                sides.append(("plain", plain_result))
            for side, result in sides:
                if result is None:
                    notes.append(f"{side} capped")
                    continue
                previous = agreed.setdefault(side, result)
                if previous != result:
                    differing = [key for key in result if previous[key] != result[key]]
                    ctx.disagreements.append(
                        f"{scope_label} / {shape_label}: {side} total {result['total']:,} "
                        f"vs {previous['total']:,} from an earlier shape "
                        f"({len(differing)} fields differ)"
                    )
                    notes.append(f"{side} DISAGREES")

            reference = overlay_result or plain_result
            cells = {"overlay": overlay}
            ratio = ""
            if not overlay_only:
                cells["plain"] = plain
                if not overlay.capped and not plain.capped and plain.ms:
                    ratio = f"x{overlay.ms / plain.ms:.1f}"

            section.add(
                shape_label, cells,
                plain="-" if overlay_only else None,
                ratio="-" if overlay_only else ratio,
                people="" if reference is None else f"{reference['total']:,}",
                notes=", ".join(notes),
            )
        yield section
