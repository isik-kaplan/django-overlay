"""Does the collapse go away if you only overlay what actually needs it?

Most of what is measured here joins two overlay views, and that shape did not
survive two conditions before the ban: the planner estimated 267,425,037,000
rows for a 132-row answer, because an appendrel parent carries no statistics
and nothing supplies a joint selectivity for two of them.

But not every entity needs to be a view. A view exists to merge a tenant row
over a vendor row -- and a label, a saved list, a campaign has no vendor row to
merge. Those are tenant-owned outright, and `docs/reference/QUERY_REWRITING.md`
records that a traversal whose target is a *plain* table costs 1.2-1.3x,
because the plain side's statistics rescue the estimate. `_m2m_fence()` agrees
by construction: it requires both the through model and the target to be view
models, and declines otherwise.

So the question this settles is whether a normalized schema is viable on the
overlay provided only the vendor-sourced entities go behind views:

    view -> view    addresses__city   (BenchAddress has a source)
    view -> plain   labels__kind      (BenchLabel does not)

Four conditions, in the combinations that matter. The estimate column is the
whole story on these shapes -- a plan that never finishes still explains
instantly, and the number it explains with is the reason it never finishes.
"""

from django.db import connection

from benchmark import harness


NAME = "hybrid"
TITLE = "Overlay only what has a vendor source: view->plain against view->view"

# Two selectivities on each side, because selectivity has mattered everywhere
# else: `kind` has four values and `name` has two hundred, the same spread as
# `country` against `city` on the view side.
VIEW_NARROW = {"addresses__city": "city0"}
# A single city reaches 200 of 1,000,000 people, which is too narrow to
# combine with anything: intersected with a 0.4% label it expects less than one
# person, and an empty result times how fast Postgres finds nothing. The
# combination cases use a hundred cities (~2%) so there is something to find.
VIEW_MID = {"addresses__city__in": [f"city{n}" for n in range(100)]}
VIEW_BROAD = {"phones__kind": "mobile"}
PLAIN_NARROW = {"labels__name": "label7"}
PLAIN_BROAD = {"labels__kind": "volunteer"}

CASES = (
    ("plain alone, narrow (view->plain)", [PLAIN_NARROW]),
    ("plain alone, broad (view->plain)", [PLAIN_BROAD]),
    ("view alone, narrow (view->view)", [VIEW_NARROW]),
    ("view alone, mid (view->view)", [VIEW_MID]),
    ("plain + plain (both plain)", [PLAIN_NARROW, PLAIN_BROAD]),
    ("view + plain, narrow plain <- the question", [VIEW_MID, PLAIN_NARROW]),
    ("view + plain, broad plain <- the question", [VIEW_MID, PLAIN_BROAD]),
    ("view + view (the known-bad)", [VIEW_MID, VIEW_BROAD]),
    ("view + view + plain", [VIEW_MID, VIEW_BROAD, PLAIN_NARROW]),
)

COLUMNS = ("fenced", "est rows", "overlay", "plain", "ratio", "rows")


def _combined(conditions):
    combined = {}
    for condition in conditions:
        combined |= condition
    return combined


def naive(model, conditions):
    """One filter, the way anyone would write it."""
    return model.objects.filter(**_combined(conditions)).values("pk").distinct().count()


def estimate(model, conditions):
    """What the planner thinks it will produce, from EXPLAIN without ANALYZE."""
    queryset = model.objects.filter(**_combined(conditions)).values("pk")
    statement, params = queryset.query.sql_with_params()
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN (FORMAT JSON) {statement}", params)
        return cursor.fetchone()[0][0]["Plan"]["Plan Rows"]


def fenced(model, conditions):
    statement, _ = model.objects.filter(**_combined(conditions)).query.sql_with_params()
    return "= ANY (ARRAY" in statement


def run(ctx):
    from tests.testapp.models import BenchPerson, PlainPerson

    section = harness.Section("One hop to a plain table vs one hop to a view", COLUMNS)
    for label, conditions in CASES:
        overlay, overlay_people = ctx.measure(lambda c=conditions: naive(BenchPerson, c))
        plain, plain_people = ctx.measure(lambda c=conditions: naive(PlainPerson, c))

        ctx.compare(label, overlay_people, plain_people)

        ratio = ""
        if not overlay.capped and not plain.capped and plain.ms:
            ratio = f"x{overlay.ms / plain.ms:.1f}"
        section.add(
            label,
            {"overlay": overlay, "plain": plain},
            fenced="yes" if fenced(BenchPerson, conditions) else "no",
            **{"est rows": f"{estimate(BenchPerson, conditions):,}"},
            ratio=ratio,
            rows="capped" if overlay_people is None else f"{overlay_people:,}",
        )
    yield section
