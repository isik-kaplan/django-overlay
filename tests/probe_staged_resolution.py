"""Never let two appendrel predicates meet in one plan.

probe_narrow_m2m_stall showed each m2m condition is well planned on its own --
`addresses__city` alone is 10ms, `phones__kind` alone is 1,038ms -- and that the
conjunction of two estimates at 267,425,037,000 rows for a 132-row answer and
never finishes. Nothing gives Postgres a joint selectivity for two predicates
over a UNION ALL view, and no fence can supply one.

So don't ask it for one. Resolve each leaf separately, where the estimate is
merely wrong rather than catastrophically wrong, and combine the id sets.
That is structurally what a "resolve the list, then use it" step already does
in the application this stands in for, so it is not a foreign shape.

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

    OVERLAY_BENCH_SCALE=1.0 POSTGRES_USER=postgres uv run pytest \\
        --reuse-db tests/probe_staged_resolution.py -s -q -o addopts="" --no-cov
"""

import time

import pytest
from django.db import OperationalError, connection

from tests.probe_bench_graph import load
from tests.testapp.models import BenchPerson, PlainPerson


pytestmark = pytest.mark.django_db(transaction=True)

CAP_MS = 20_000

NARROW = {"addresses__city": "city0"}
BROAD = {"phones__kind": "mobile"}
THIRD = {"emails__domain": "example.com"}


def cap(milliseconds):
    with connection.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = {milliseconds}")
        cursor.execute("SET lock_timeout = 5000")


def ids_for(model, condition):
    return set(model.objects.filter(**condition).values_list("pk", flat=True))


def naive(model, conditions):
    """Everything in one filter. The shape that does not finish."""
    combined = {}
    for condition in conditions:
        combined |= condition
    return len(set(model.objects.filter(**combined).values_list("pk", flat=True)))


def narrow_first(model, conditions):
    """Resolve the leaf given first, then apply the rest to its ids."""
    head, *rest = conditions
    ids = list(ids_for(model, head))
    combined = {}
    for condition in rest:
        combined |= condition
    if not combined:
        return len(ids)
    return len(set(model.objects.filter(pk__in=ids, **combined).values_list("pk", flat=True)))


def broad_first(model, conditions):
    """The same, worst order -- resolve the least selective leaf first."""
    return narrow_first(model, list(reversed(conditions)))


def subquery_first(model, conditions):
    """Narrow-first without materialising ids in Python."""
    head, *rest = conditions
    combined = {}
    for condition in rest:
        combined |= condition
    scoped = model.objects.filter(**head).values("pk")
    if not combined:
        return model.objects.filter(pk__in=scoped).values("pk").distinct().count()
    return len(
        set(model.objects.filter(pk__in=scoped, **combined).values_list("pk", flat=True))
    )


def intersect(model, conditions):
    """Every leaf independently, combined as Python sets. No ordering to guess."""
    resolved = None
    for condition in conditions:
        found = ids_for(model, condition)
        resolved = found if resolved is None else (resolved & found)
    return len(resolved)


STRATEGIES = (("naive (one filter)", naive), ("narrow-first", narrow_first),
              ("broad-first", broad_first), ("subquery-first", subquery_first),
              ("intersect leaves", intersect))

CASES = (
    ("2 leaves: city + phones", [NARROW, BROAD]),
    ("3 leaves: city + phones + emails", [NARROW, BROAD, THIRD]),
)


def timed(build):
    started = time.perf_counter()
    try:
        value = build()
    except OperationalError:
        return float(CAP_MS), None
    return (time.perf_counter() - started) * 1000, value


def test_staged_resolution():
    load()
    cap(CAP_MS)

    for case_label, conditions in CASES:
        print("\n\n" + "=" * 104)
        print(case_label)
        print("=" * 104)
        print(f"  {'strategy':<24} {'overlay':>11} {'plain':>10} {'ratio':>9}   people")
        print("  " + "-" * 92)

        # The plain table answers every variant, so it supplies the truth that
        # each overlay variant is checked against. A staged strategy that is
        # fast because it dropped a condition would otherwise look like a win.
        truth = None
        for label, strategy in STRATEGIES:
            overlay_ms, overlay_people = timed(lambda s=strategy, c=conditions: s(BenchPerson, c))
            plain_ms, plain_people = timed(lambda s=strategy, c=conditions: s(PlainPerson, c))
            if truth is None:
                truth = plain_people

            if overlay_people is None:
                cells = f"{'>' + str(CAP_MS // 1000) + 's':>11} {plain_ms:>8.0f}ms {'':>9}"
                note = "did not finish"
            else:
                ratio = overlay_ms / plain_ms if plain_ms else float("nan")
                cells = f"{overlay_ms:>9.0f}ms {plain_ms:>8.0f}ms x{ratio:>8.1f}"
                note = f"{overlay_people:,}"
            for name, people in (("overlay", overlay_people), ("plain", plain_people)):
                if people is not None and truth is not None and people != truth:
                    note += f"   {name} WRONG (expected {truth:,})"
            print(f"  {label:<24} {cells}   {note}")
