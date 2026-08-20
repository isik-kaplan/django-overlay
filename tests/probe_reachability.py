"""Is `addresses__country='GB'` really empty, and if so, whose fault is it?

`probe_multi_m2m` measured zero people for that filter on data where a quarter
of all addresses are GB. Two very different explanations:

  * the link generator's `CASE WHEN g %% 2` parity interacts with the
    `g %% 4` that picks the country, so no *reachable* address is GB — a
    benchmark-data artifact, and the fix is to choose different constants;
  * the m2m fence drops rows — a correctness bug in the library.

So ask three ways and compare: raw SQL over the views, the ORM with the
rewrite off, and the ORM with the fence on. Raw SQL is the ground truth.

    OVERLAY_BENCH_SCALE=0.3 POSTGRES_USER=postgres uv run pytest \\
        tests/probe_reachability.py -s -q -o addopts="" --no-cov
"""

import pytest
from django.db import connection
from django.test import override_settings

from benchmark.graph import load
from tests.testapp.models import BenchPerson


pytestmark = pytest.mark.django_db(transaction=True)

OFF = override_settings(DJANGO_OVERLAY_REWRITE_TRAVERSALS=False)


def raw(statement):
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return cursor.fetchone()[0]


def test_reachability():
    load()

    print("\n\n" + "=" * 96)
    print("WHAT IS ACTUALLY IN THE ADDRESS VIEW")
    print("=" * 96)
    with connection.cursor() as cursor:
        cursor.execute("SELECT country, count(*) FROM bench_address_view GROUP BY 1 ORDER BY 2 DESC")
        for country, count in cursor.fetchall():
            print(f"  {country:<12} {count:>10,}")

    print("\n" + "=" * 96)
    print("WHAT IS REACHABLE THROUGH THE LINK TABLE (raw SQL, ground truth)")
    print("=" * 96)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT a.country, count(DISTINCT l.person_id) "
            "FROM bench_person_address_view l "
            "JOIN bench_address_view a ON a.id = l.address_id "
            "GROUP BY 1 ORDER BY 2 DESC"
        )
        for country, people in cursor.fetchall():
            print(f"  {country:<12} {people:>10,} people")

    print("\n" + "=" * 96)
    print("THREE WAYS TO ASK THE SAME QUESTION")
    print("=" * 96)
    print(f"  {'filter':<34} {'raw SQL':>12} {'ORM unfenced':>14} {'ORM fenced':>12}")
    print("  " + "-" * 78)
    for label, column, value in (
        ("addresses__country", "a.country", "GB"),
        ("addresses__country", "a.country", "US"),
        ("addresses__postcode", "a.postcode", "pc42"),
    ):
        truth = raw(
            f"SELECT count(DISTINCT l.person_id) FROM bench_person_address_view l "  # noqa: S608
            f"JOIN bench_address_view a ON a.id = l.address_id WHERE {column} = '{value}'"
        )
        lookup = {f"{label}": value}
        with OFF:
            unfenced = BenchPerson.objects.filter(**lookup).values("pk").distinct().count()
        fenced = BenchPerson.objects.filter(**lookup).values("pk").distinct().count()
        verdict = "" if truth == unfenced == fenced else "   <-- MISMATCH"
        print(f"  {label}={value!r:<12} {truth:>12,} {unfenced:>14,} {fenced:>12,}{verdict}")

    print("\n" + "=" * 96)
    print("PHONE KINDS, FOR COMPARISON (this one did match)")
    print("=" * 96)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.kind, count(DISTINCT l.person_id) "
            "FROM bench_person_phone_view l JOIN bench_phone_view p ON p.id = l.phone_id "
            "GROUP BY 1 ORDER BY 2 DESC"
        )
        for kind, people in cursor.fetchall():
            print(f"  {kind:<12} {people:>10,} people")
