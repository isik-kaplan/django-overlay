"""Key lists read into Python and sent back: what the round trip costs.

Code reads a set of keys -- a scope, a page's parents, the rows a label covers
-- and filters by them in a second query: `filter(pk__in=ids)`. On a uuid key
Django prepares that list one value at a time, twice, and psycopg2 adapts each
value on its own, so past a few thousand keys the statement costs more in
Python than in Postgres.

Four arms, all returning the same rows, each timed from reading the keys to
having the second query's rows -- because the round trip is the thing measured:

- **IN list**: Django's own `IN`, the array-literal rewrite off.
- **one array**: the same list sent as one array parameter,
  `pk = ANY (%s::uuid[])`. The plan is unchanged; only the per-value work goes.
- **kept in SQL**: no round trip -- the scope passed as a queryset and fenced,
  `pk__overlay_selective_in=scope.values("pk")`. What to write when the keys
  never needed to be in Python.
- **plain**: the IN list against the plain mirror, whose keys are uuids too.
"""

from django.test import override_settings

from benchmark import harness


NAME = "lists"
TITLE = "Key lists read into Python and sent back: IN list, one array, or kept in SQL"

OFF = override_settings(DJANGO_OVERLAY_ARRAY_LITERAL_IN=False)

# `city` carries 1,000 distinct values over the people, so N cities is about
# N thousand keys at scale 1.0.
SCOPES = (
    ("1 city", 1),
    ("10 cities", 10),
    ("100 cities", 100),
    ("500 cities", 500),
)

COLUMNS = ("IN list", "one array", "array gain", "kept in SQL", "SQL gain", "plain", "rows")


def _cities(count):
    return [f"city{n}" for n in range(count)]


def round_trip(model, cities):
    """Read the keys, then fetch those rows by key."""
    keys = list(model.objects.filter(city__in=cities).values_list("pk", flat=True))
    return len(list(model.objects.filter(pk__in=keys).values_list("pk", "postcode")))


def kept_in_sql(model, cities):
    scope = model.objects.filter(city__in=cities).values("pk")
    return len(list(model.objects.filter(pk__overlay_selective_in=scope).values_list("pk", "postcode")))


def row(ctx, section, label, cities, models):
    overlay_model, plain_model = models

    with OFF:
        in_list, in_list_value = ctx.measure(lambda: round_trip(overlay_model, cities))
    one_array, one_array_value = ctx.measure(lambda: round_trip(overlay_model, cities))
    in_sql, in_sql_value = ctx.measure(lambda: kept_in_sql(overlay_model, cities))
    plain, plain_value = ctx.measure(lambda: round_trip(plain_model, cities))

    ctx.compare(f"{label} (one array vs IN list)", in_list_value, one_array_value, "the array form changed the result")
    ctx.compare(
        f"{label} (kept in SQL vs IN list)", in_list_value, in_sql_value, "keeping it in SQL changed the result"
    )
    ctx.compare(f"{label} (overlay vs plain)", in_list_value, plain_value)

    section.add(
        label,
        {"IN list": in_list, "one array": one_array, "kept in SQL": in_sql, "plain": plain},
        **{"array gain": harness.gain(in_list, one_array), "SQL gain": harness.gain(in_list, in_sql)},
        rows="capped" if in_list_value is None else f"{in_list_value:,}",
    )


def run(ctx):
    from tests.testapp.models import BenchPerson, PlainPerson

    models = (BenchPerson, PlainPerson)
    for attempt in range(1, ctx.passes + 1):
        suffix = f" (pass {attempt})" if ctx.passes > 1 else ""
        section = harness.Section(
            f"Read the keys, fetch the rows by them{suffix}",
            COLUMNS,
            note="each gain is that column against Django's IN list; times include the Python side",
        )
        for label, count in SCOPES:
            row(ctx, section, label, _cities(count), models)
        yield section
