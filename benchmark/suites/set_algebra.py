"""Can the leaf-by-leaf strategy stay in the database?

The staged suite finds that resolving each m2m leaf separately and
intersecting the id sets in Python is the only strategy that survives three
leaves. The cost is client memory: ~19MB per broad leaf, ~57MB for three.

The obvious reading of that result is "Django had to pull the ids into
Python". That reading is wrong. `filter(pk__in=<queryset>)` compiles to a SQL
subquery and moves nothing, and the staged suite's `subquery-first` variant
does exactly that -- and still runs past the cap. What the Python round trip
actually buys is not data movement but *planner information*: a literal list of
200 ids has an exact, visible cardinality, and a subquery over a UNION ALL view
has none.

So the question is whether anything gives the planner that same visible
cardinality without leaving the database. Two candidates:

  fenced-array   `= ANY (ARRAY(subquery))` is an InitPlan -- evaluated once,
                 before the outer plan is costed, so the outer query sees a
                 constant array rather than a relation it cannot size. This is
                 the m2m fence's own mechanism, applied to whole leaves.
  INTERSECT      set operations plan each branch separately, which is the very
                 property that makes leaf-by-leaf work in Python. If that
                 survives being expressed as SQL, the memory ceiling is gone
                 and the result stays a lazy queryset.

Both are measured against the Python intersection that already works.
"""

from django.db.models import Q

from benchmark import harness


NAME = "set_algebra"
TITLE = "Keeping the leaf-by-leaf strategy inside the database"

LEAVES = (
    {"addresses__city": "city0"},
    {"phones__kind": "mobile"},
    {"emails__domain": "example.com"},
)

COLUMNS = ("overlay", "plain", "ratio", "people")


def python_intersection(model, leaves):
    """The known-good baseline: every leaf resolved, combined as Python sets."""
    resolved = None
    for leaf in leaves:
        found = set(model.objects.filter(**leaf).values_list("pk", flat=True))
        resolved = found if resolved is None else (resolved & found)
    return len(resolved)


def fenced_arrays(model, leaves):
    """One `= ANY (ARRAY(subquery))` per leaf, all in one statement.

    Each InitPlan is evaluated once and collapses to a constant array, so the
    outer query gets a countable predicate per leaf instead of a join it cannot
    estimate -- the same trick the m2m fence plays, applied a level up.
    """
    queryset = model.objects.all()
    for leaf in leaves:
        inner = model.objects.filter(**leaf).values("pk")
        queryset = queryset.filter(Q(pk__overlay_fenced_in=inner))
    return queryset.values("pk").distinct().count()


def plain_subqueries(model, leaves):
    """The same shape with ordinary `pk__in`, as the control."""
    queryset = model.objects.all()
    for leaf in leaves:
        queryset = queryset.filter(Q(pk__in=model.objects.filter(**leaf).values("pk")))
    return queryset.values("pk").distinct().count()


def sql_intersect(model, leaves):
    """`INTERSECT` between the leaves, planned branch by branch."""
    branches = [model.objects.filter(**leaf).values("pk").distinct() for leaf in leaves]
    combined = branches[0].intersection(*branches[1:])
    return len(list(combined))


STRATEGIES = (
    ("python intersection", python_intersection, True),
    ("fenced arrays (SQL)", fenced_arrays, False),
    ("plain pk__in (SQL)", plain_subqueries, True),
    ("INTERSECT (SQL)", sql_intersect, True),
)

CASES = (("2 leaves", LEAVES[:2]), ("3 leaves", LEAVES))


def run(ctx):
    from tests.testapp.models import BenchPerson, PlainPerson

    for case_label, leaves in CASES:
        section = harness.Section(
            f"{case_label}: " + " AND ".join(next(iter(leaf)) for leaf in leaves), COLUMNS,
        )
        truth = None
        for label, strategy, has_plain in STRATEGIES:
            overlay, overlay_people = ctx.measure(
                lambda s=strategy, ls=leaves: s(BenchPerson, ls), rounds=2)
            if has_plain:
                plain, plain_people = ctx.measure(
                    lambda s=strategy, ls=leaves: s(PlainPerson, ls), rounds=2)
            else:
                # `overlay_fenced_in` resolves only through OverlayQuery, by
                # design -- a plain model has no way to express this row.
                plain, plain_people = None, None
            if truth is None and plain_people is not None:
                truth = plain_people

            if overlay_people is not None and truth is not None and overlay_people != truth:
                ctx.disagreements.append(
                    f"{case_label} / {label}: overlay found {overlay_people:,} people, "
                    f"expected {truth:,}"
                )

            cells = {"overlay": overlay}
            ratio = "-"
            if has_plain:
                cells["plain"] = plain
                ratio = ""
                if not overlay.capped and not plain.capped and plain.ms:
                    ratio = f"x{overlay.ms / plain.ms:.1f}"
            section.add(
                label, cells,
                plain=None if has_plain else "-",
                ratio=ratio,
                people="did not finish" if overlay_people is None else f"{overlay_people:,}",
            )
        yield section
