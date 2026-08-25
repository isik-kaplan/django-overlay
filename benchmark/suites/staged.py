"""Never let two appendrel predicates meet in one plan.

Each m2m condition is well planned on its own -- `addresses__city` alone is
10ms, `phones__kind` alone is 1,038ms -- and the conjunction of two estimates
at 267,425,037,000 rows for a 132-row answer. Nothing gives Postgres a joint
selectivity for two predicates over a UNION ALL view, and no fence can supply
one.

So don't ask it for one. Resolve each leaf separately, where the estimate is
merely wrong rather than catastrophically wrong, and combine the id sets. That
is structurally what a "resolve the list, then use it" step already does in the
application this stands in for, so it is not a foreign shape.

The variants differ in *how* the leaves combine, which is the whole question:

  narrow-first   run the selective leaf, then apply the rest as ordinary
                 filters against its ids. One extra query, and the second
                 plan sees a small literal set.
  broad-first    the same, in the wrong order -- included because if order
                 does not matter the library could skip having to guess it,
                 and if it does, that guess is the hard part.
  subquery       narrow-first without pulling ids into Python. Cheaper if it
                 works, but the subquery is an appendrel predicate again.
  intersect      every leaf resolved independently, combined as Python sets.
                 No ordering to guess at all.

Also run at three leaves, because a real saved search is three or four
conditions deep and two working proves nothing about four.
"""

from benchmark import harness


NAME = "staged"
TITLE = "Resolving a saved search leaf by leaf instead of all at once"

NARROW = {"addresses__city": "city0"}
BROAD = {"phones__kind": "mobile"}
THIRD = {"emails__domain": "example.com"}

COLUMNS = ("overlay", "plain", "ratio", "people")


def ids_for(model, condition):
    return set(model.objects.filter(**condition).values_list("pk", flat=True))


def _combined(conditions):
    combined = {}
    for condition in conditions:
        combined |= condition
    return combined


def naive(model, conditions):
    """Everything in one filter. The shape that does not finish."""
    return len(set(model.objects.filter(**_combined(conditions)).values_list("pk", flat=True)))


def narrow_first(model, conditions):
    """Resolve the leaf given first, then apply the rest to its ids."""
    head, *rest = conditions
    ids = list(ids_for(model, head))
    combined = _combined(rest)
    if not combined:
        return len(ids)
    return len(set(model.objects.filter(pk__in=ids, **combined).values_list("pk", flat=True)))


def broad_first(model, conditions):
    """The same, worst order -- resolve the least selective leaf first."""
    return narrow_first(model, list(reversed(conditions)))


def subquery_first(model, conditions):
    """Narrow-first without materialising ids in Python."""
    head, *rest = conditions
    combined = _combined(rest)
    scoped = model.objects.filter(**head).values("pk")
    if not combined:
        return model.objects.filter(pk__in=scoped).values("pk").distinct().count()
    return len(set(model.objects.filter(pk__in=scoped, **combined).values_list("pk", flat=True)))


def intersect(model, conditions):
    """Every leaf independently, combined as Python sets. No ordering to guess."""
    resolved = None
    for condition in conditions:
        found = ids_for(model, condition)
        resolved = found if resolved is None else (resolved & found)
    return len(resolved)


STRATEGIES = (
    ("naive (one filter)", naive),
    ("narrow-first", narrow_first),
    ("broad-first", broad_first),
    ("subquery-first", subquery_first),
    ("intersect leaves", intersect),
)

CASES = (
    ("2 leaves: city + phones", [NARROW, BROAD]),
    ("3 leaves: city + phones + emails", [NARROW, BROAD, THIRD]),
)


def run(ctx):
    from tests.testapp.models import BenchPerson, PlainPerson

    for case_label, conditions in CASES:
        section = harness.Section(case_label, COLUMNS)

        # The plain table answers every variant, so it supplies the truth each
        # overlay variant is checked against. A staged strategy that is fast
        # because it dropped a condition would otherwise look like a win.
        truth = None
        for label, strategy in STRATEGIES:
            overlay, overlay_people = ctx.measure(lambda s=strategy, c=conditions: s(BenchPerson, c), rounds=2)
            plain, plain_people = ctx.measure(lambda s=strategy, c=conditions: s(PlainPerson, c), rounds=2)
            if truth is None:
                truth = plain_people

            for side, people in (("overlay", overlay_people), ("plain", plain_people)):
                if people is not None and truth is not None and people != truth:
                    ctx.disagreements.append(
                        f"{case_label} / {label}: {side} found {people:,} people, expected {truth:,}"
                    )

            ratio = ""
            if not overlay.capped and not plain.capped and plain.ms:
                ratio = f"x{overlay.ms / plain.ms:.1f}"
            section.add(
                label,
                {"overlay": overlay, "plain": plain},
                ratio=ratio,
                people="did not finish" if overlay_people is None else f"{overlay_people:,}",
            )
        yield section
