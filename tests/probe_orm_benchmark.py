"""What ordinary Django code costs on this schema.

Every measurement is ORM against ORM: the same queryset written twice, once
against the overlay graph and once against `PlainPerson` and friends — ordinary
models, ordinary tables, ordinary `ManyToManyField`, same fields, same indexes,
same rows. So the ratio is the view and nothing else.

The graph is the production shape:

    BenchPerson / Address / Phone / Email    overridable, soft_delete
    BenchPersonAddress / Phone / Email       overridable=False, soft_delete=False

Three sections:

  A. what ordinary code costs, with every rewrite active
  B. what the library refuses, and what to write instead
  C. what is still slow and cannot be rewritten away

    OVERLAY_BENCH_SCALE=0.3 POSTGRES_USER=postgres uv run pytest \\
        tests/probe_orm_benchmark.py -s -q -o addopts="" --no-cov
"""

import time

import pytest
from django.db import connection
from django.db.models import Avg, Count, Max
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from benchmark.graph import load
from django_overlay.exceptions import OverlayConfigurationError
from tests.testapp.models import (
    BenchAddress,
    BenchPerson,
    BenchPersonAddress,
    BenchPersonPhone,
    BenchPhone,
    PlainPerson,
    PlainPersonAddress,
    PlainPersonPhone,
)


pytestmark = pytest.mark.django_db(transaction=True)

TIMEOUT_MS = 30_000


def timed(build, rounds=3):
    """(best milliseconds, rows, query count)."""
    best, rows, queries = None, None, 0
    for _ in range(rounds):
        started = time.perf_counter()
        with CaptureQueriesContext(connection) as captured:
            result = build()
            rows = len(list(result)) if hasattr(result, "__iter__") else result
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
        queries = len(captured)
        if best > 4000:
            break
    return best, rows, queries


def row(label, overlay_build, plain_build, note=""):
    overlay_ms, overlay_rows, overlay_q = timed(overlay_build)
    plain_ms, plain_rows, plain_q = timed(plain_build)
    ratio = overlay_ms / plain_ms if plain_ms else float("nan")
    flag = "" if overlay_rows == plain_rows else "  ROWS DIFFER"
    queries = f"{plain_q}->{overlay_q}" if overlay_q != plain_q else str(overlay_q)
    print(f"  {label:<46} {overlay_ms:>9.1f}ms {plain_ms:>9.1f}ms  x{ratio:>8.2f}  "
          f"{queries:>7}  {note}{flag}")
    return ratio


def header():
    print(f"  {'ORM expression':<46} {'overlay':>11} {'plain':>11}  {'ratio':>9}  {'queries':>7}")
    print("  " + "-" * 104)


def test_orm_benchmark():
    started = time.perf_counter()
    load()
    print(f"\n\nloaded in {time.perf_counter() - started:.0f}s")
    print(f"  people {BenchPerson.objects.count():,}   addresses {BenchAddress.objects.count():,}   "
          f"links {BenchPersonAddress.objects.count():,}")

    person = BenchPerson.objects.order_by("id").first()
    plain_person = PlainPerson.objects.order_by("id").first()

    print("\n" + "=" * 108)
    print("A. ORDINARY ORM CODE  (every rewrite active)")
    print("=" * 108)
    header()

    print("  -- reads on one model")
    row("get(pk=…)",
        lambda: BenchPerson.objects.get(pk=person.pk),
        lambda: PlainPerson.objects.get(pk=plain_person.pk))
    row("filter(city=…)[:100]",
        lambda: BenchPerson.objects.filter(city="city42").order_by("id")[:100],
        lambda: PlainPerson.objects.filter(city="city42").order_by("id")[:100])
    row("filter(born_on=…)[:100]   (unindexed column)",
        lambda: BenchPerson.objects.filter(born_on="1970-01-01").order_by("id")[:100],
        lambda: PlainPerson.objects.filter(born_on="1970-01-01").order_by("id")[:100])
    row("filter(city=…).order_by('-score')[:20]",
        lambda: BenchPerson.objects.filter(city="city42").order_by("-score")[:20],
        lambda: PlainPerson.objects.filter(city="city42").order_by("-score")[:20])
    row("filter(status=…).exists()",
        lambda: BenchPerson.objects.filter(status="active").exists(),
        lambda: PlainPerson.objects.filter(status="active").exists())
    row("count()",
        lambda: BenchPerson.objects.count(),
        lambda: PlainPerson.objects.count())
    row("filter(city=…).count()",
        lambda: BenchPerson.objects.filter(city="city42").count(),
        lambda: PlainPerson.objects.filter(city="city42").count())
    row("aggregate(Max, Avg)",
        lambda: BenchPerson.objects.aggregate(Max("score"), Avg("score"))["score__max"],
        lambda: PlainPerson.objects.aggregate(Max("score"), Avg("score"))["score__max"])
    row("values_list('id', flat=True)[:1000]",
        lambda: BenchPerson.objects.order_by("id").values_list("id", flat=True)[:1000],
        lambda: PlainPerson.objects.order_by("id").values_list("id", flat=True)[:1000])
    row("filter(pk__in=[100 literal ids])",
        lambda: BenchPerson.objects.filter(
            pk__in=list(BenchPerson.objects.order_by("id").values_list("pk", flat=True)[:100])),
        lambda: PlainPerson.objects.filter(
            pk__in=list(PlainPerson.objects.order_by("id").values_list("pk", flat=True)[:100])))

    print("\n  -- traversals  (m2m fence)")
    row("filter(phones__number=…)",
        lambda: BenchPerson.objects.filter(phones__number="+447000000042").order_by("id"),
        lambda: PlainPerson.objects.filter(phones__number="+447000000042").order_by("id"))
    row("filter(addresses__city=…)[:100]",
        lambda: BenchPerson.objects.filter(addresses__city="city42").order_by("id")[:100],
        lambda: PlainPerson.objects.filter(addresses__city="city42").order_by("id")[:100])
    row("filter(addresses__postcode=…)",
        lambda: BenchPerson.objects.filter(addresses__postcode="pc42").order_by("id"),
        lambda: PlainPerson.objects.filter(addresses__postcode="pc42").order_by("id"))
    row("filter(person__city=…) on the link  (fk rewrite)",
        lambda: BenchPersonAddress.objects.filter(person__city="city42").order_by("id")[:100],
        lambda: PlainPersonAddress.objects.filter(person__city="city42").order_by("id")[:100])

    print("\n  -- the detail page  (select_related routing)")
    row("person.addresses.all()",
        lambda: person.addresses.all(),
        lambda: plain_person.addresses.all())
    row("person.phones.all()",
        lambda: person.phones.all(),
        lambda: plain_person.phones.all())
    row("prefetch_related('addresses','phones','emails')[:50]",
        lambda: BenchPerson.objects.prefetch_related(
            "addresses", "phones", "emails").order_by("id")[:50],
        lambda: PlainPerson.objects.prefetch_related(
            "addresses", "phones", "emails").order_by("id")[:50])
    row("link.select_related('person','phone')[:100]",
        lambda: BenchPersonPhone.objects.select_related("person", "phone").order_by("id")[:100],
        lambda: PlainPersonPhone.objects.select_related("person", "phone").order_by("id")[:100])

    print("\n  -- grouping")
    row("filter(city=…).annotate(Count('addresses'))[:20]",
        lambda: BenchPerson.objects.filter(city="city42").annotate(
            n=Count("addresses")).order_by("id")[:20],
        lambda: PlainPerson.objects.filter(city="city42").annotate(
            n=Count("addresses")).order_by("id")[:20])

    print("\n  -- writes")
    row("create()",
        lambda: BenchPerson.objects.create(first_name="w", last_name="w", city="c",
                                           postcode="p", status="active", score=1),
        lambda: PlainPerson.objects.create(id=__import__("uuid").uuid4(), first_name="w",
                                           last_name="w", city="c", postcode="p",
                                           status="active", score=1))
    row("filter(pk=…).update(score=…)",
        lambda: BenchPerson.objects.filter(pk=person.pk).update(score=7),
        lambda: PlainPerson.objects.filter(pk=plain_person.pk).update(score=7))

    print("\n" + "=" * 108)
    print("B. WHAT THE LIBRARY REFUSES  (and what to write instead)")
    print("=" * 108)
    refusals = [
        ("select_for_update()",
         lambda: list(BenchPerson.objects.select_for_update().filter(pk=person.pk)),
         "FOR UPDATE locks nothing on a view. Lock the base table, or use an advisory lock."),
        ("select_related(…).iterator()",
         lambda: list(BenchPersonPhone.objects.select_related("person").iterator()),
         "Pass chunk_size, or drop select_related and let the prefetch run."),
        ("select_related(…).union(…)",
         lambda: BenchPersonPhone.objects.select_related("person").union(
             BenchPersonPhone.objects.all()),
         "Fetch the related rows in a second query."),
        ("union(…).select_related(…)",
         lambda: BenchPersonPhone.objects.all().union(
             BenchPersonPhone.objects.all()).select_related("person"),
         "Same — the join cannot be routed after a combinator."),
    ]
    for label, build, remedy in refusals:
        try:
            build()
            print(f"  {label:<44} NOT REFUSED -- check this")
        except (OverlayConfigurationError, Exception) as error:  # noqa: BLE001
            kind = type(error).__name__
            print(f"  {label:<44} {kind:<28} {remedy}")

    print("\n" + "=" * 108)
    print("C. STILL SLOW, AND NOT FIXABLE BY REWRITING")
    print("=" * 108)
    header()
    row("order_by('-score')[:20]        UNSCOPED",
        lambda: BenchPerson.objects.order_by("-score")[:20],
        lambda: PlainPerson.objects.order_by("-score")[:20],
        note="anti-join + soft-delete qual")
    row("order_by('-score')[100000:100020]  deep",
        lambda: BenchPerson.objects.order_by("-score")[100000:100020],
        lambda: PlainPerson.objects.order_by("-score")[100000:100020],
        note="no early exit")
    row("filter(phones__kind='mobile')[:200]  broad m2m",
        lambda: BenchPerson.objects.filter(phones__kind="mobile").order_by("id")[:200],
        lambda: PlainPerson.objects.filter(phones__kind="mobile").order_by("id")[:200],
        note="fence helps 8x, still slow")
    row("exclude(addresses__city=…)[:100]",
        lambda: BenchPerson.objects.exclude(addresses__city="city42").order_by("id")[:100],
        lambda: PlainPerson.objects.exclude(addresses__city="city42").order_by("id")[:100],
        note="negation: no rewrite is sound")
    row("distinct().order_by('-score')[:20]",
        lambda: BenchPerson.objects.distinct().order_by("-score")[:20],
        lambda: PlainPerson.objects.distinct().order_by("-score")[:20],
        note="unscoped ordering again")

    print("\n" + "=" * 108)
    print("D. WHY IS phones__kind='mobile' SO SLOW?")
    print("=" * 108)
    phones_matching = BenchPhone.objects.filter(kind="mobile").count()
    links_matching = BenchPersonPhone.objects.filter(phone__kind="mobile").count()
    people_matching = BenchPerson.objects.filter(phones__kind="mobile").count()
    print(f"  selectivity: {phones_matching:,} phones -> {links_matching:,} links -> "
          f"{people_matching:,} people, out of {BenchPerson.objects.count():,}")
    print("  The query matches ~40% of the table, so no index strategy can help: an index")
    print("  is a way to avoid reading rows, and here the work is proportional to the")
    print("  answer. The rows below decompose what the time is actually spent on --")
    print("  drop the ORDER BY and the same filter costs a twentieth as much, so the")
    print("  dominant cost is unscoped ordering, not the join and not the fence.\n")
    header()
    row("order_by('id')[:200]          (as measured above)",
        lambda: BenchPerson.objects.filter(phones__kind="mobile").order_by("id")[:200],
        lambda: PlainPerson.objects.filter(phones__kind="mobile").order_by("id")[:200],
        note="broad + unscoped ordering")
    row("[:200] with NO ordering",
        lambda: BenchPerson.objects.filter(phones__kind="mobile")[:200],
        lambda: PlainPerson.objects.filter(phones__kind="mobile")[:200],
        note="isolates the ordering cost")
    row("filter(city=…) FIRST, then phones__kind",
        lambda: BenchPerson.objects.filter(
            city="city42", phones__kind="mobile").order_by("id")[:200],
        lambda: PlainPerson.objects.filter(
            city="city42", phones__kind="mobile").order_by("id")[:200],
        note="<- the question")
    row("filter(last_name=…) FIRST, then phones__kind",
        lambda: BenchPerson.objects.filter(
            last_name="last42", phones__kind="mobile").order_by("id")[:200],
        lambda: PlainPerson.objects.filter(
            last_name="last42", phones__kind="mobile").order_by("id")[:200],
        note="a narrower scope still")
    row("filter(phones__kind=…).exists()",
        lambda: BenchPerson.objects.filter(phones__kind="mobile").exists(),
        lambda: PlainPerson.objects.filter(phones__kind="mobile").exists())

    # Worth checking rather than assuming: the fence materialises a six-figure
    # id set here, which looks like it ought to cost more than it saves. It
    # does not -- it is still worth 5-8x at this selectivity. What it does not
    # do is shrink when the outer query is scoped, because its subquery is not
    # correlated with the outer filter: the same array is built either way.
    print("\n  the same query with the fence turned off, to show what it is worth here:")
    with override_settings(DJANGO_OVERLAY_REWRITE_TRAVERSALS=False):
        unfenced_broad, _, _ = timed(
            lambda: BenchPerson.objects.filter(phones__kind="mobile").order_by("id")[:200])
        unfenced_scoped, _, _ = timed(
            lambda: BenchPerson.objects.filter(
                city="city42", phones__kind="mobile").order_by("id")[:200])
    fenced_broad, _, _ = timed(
        lambda: BenchPerson.objects.filter(phones__kind="mobile").order_by("id")[:200])
    fenced_scoped, _, _ = timed(
        lambda: BenchPerson.objects.filter(
            city="city42", phones__kind="mobile").order_by("id")[:200])
    print(f"    broad,  unfenced {unfenced_broad:>9.1f}ms   fenced {fenced_broad:>9.1f}ms   "
          f"x{unfenced_broad / fenced_broad:.1f}")
    print(f"    scoped, unfenced {unfenced_scoped:>9.1f}ms   fenced {fenced_scoped:>9.1f}ms   "
          f"x{unfenced_scoped / fenced_scoped:.1f}")
