"""What the late-row-lookup rewrite is worth, on the shapes a list endpoint emits.

The gap this fills is specific. `shapes` measures the view in raw SQL, so an
ORM-level rewrite is invisible to it by construction. `ban` runs through the
ORM but its only paged shape filters on an m2m relation, which multiplies rows
-- so the rewrite correctly declines it and measures as exactly zero. Between
them the two suites could not see this mechanism at all, and its only numbers
came from a probe run by hand.

The rewrite resolves an ordered page's primary keys first and fetches those
rows by key, because the cost of an ordered page is not the sorting. The sort
is a top-N heapsort holding 31kB; what costs is that the rows arrive unordered,
so every one of them is produced and passed through it carrying full width.
Selecting only the key measured 40ms where selecting the row measured 335ms.

Three columns: the rewrite off, the rewrite on, and a plain non-overlay table
holding identical rows. `overlay_direct()` is the off arm rather than
`override_settings` -- it is the documented per-query opt-out, so measuring
through it prices the thing callers can actually reach, and a run where the
lever silently stopped working would show up here as two identical columns.

The axis that matters is **scope breadth**, not page size. The rewrite saves
the scan that feeds the sort, so its value tracks how many rows qualify before
the LIMIT applies: unscoped is the whole view, and a selective filter leaves
almost nothing to defer. Page size barely moves it, which the second section
is there to demonstrate rather than assert.
"""

from benchmark import harness


NAME = "pagination"
TITLE = "The ordered page: what resolving keys first is worth"

COLUMNS = ("direct", "rewritten", "gain", "plain", "rows")

# Fractions of the table rather than row counts, so the suite says the same
# thing at every scale. BenchPerson holds 1,000 cities spread evenly, so N
# cities is N/1000 of the view -- the fence's docstring is the long version of
# why a fraction is the right unit here.
UNSCOPED = {}
SCOPE_10_PERCENT = {"city__in": [f"city{n}" for n in range(100)]}
SCOPE_1_PERCENT = {"city__in": [f"city{n}" for n in range(10)]}
SCOPE_TENTH_PERCENT = {"city": "city0"}

PAGE_SIZE = 50


def _arm(queryset, direct):
    """The unrewritten arm, where the model has one.

    The plain mirror is an ordinary Django model with no `overlay_direct()`, so
    its two arms are the same queryset -- which is the point of measuring it:
    whatever gap shows up in the plain column is drift, and no gain in the
    overlay columns means anything unless it clears that.
    """
    if direct and hasattr(queryset, "overlay_direct"):
        return queryset.overlay_direct()
    return queryset


def page(model, scope, direct, size=PAGE_SIZE, offset=0):
    """The query `ListAPIView` emits: filter, order, slice."""
    queryset = _arm(model.objects.filter(**scope).order_by("id"), direct)
    return len(list(queryset[offset : offset + size]))


def keyset_page(model, scope, direct, cursor, size=PAGE_SIZE):
    """What `CursorPagination` emits, which is a page with one more predicate.

    Worth its own row because the predicate changes the plan rather than the
    shape: `id > cursor` is what lets the anti-join become a merge instead of a
    hash, and a cursor deep in the table leaves little enough qualifying that
    the rewrite has nothing left to save. So this row should show the gain
    shrinking, and that is the honest account of when to stop caring.
    """
    queryset = _arm(model.objects.filter(pk__gt=cursor, **scope).order_by("id"), direct)
    return len(list(queryset[:size]))


def count(model, scope, direct):
    """What `PageNumberPagination` adds on top of every page.

    Unaffected by the rewrite -- no slice, so it never fires -- and measured
    anyway, because it is half of what a page-number paginator costs and the
    reason `CursorPagination` is the cheaper default here.
    """
    del direct
    return model.objects.filter(**scope).count()


def row(ctx, section, label, operation, models, rows="", **kwargs):
    overlay_model, plain_model = models

    direct, direct_value = ctx.measure(lambda: operation(overlay_model, direct=True, **kwargs))
    rewritten, rewritten_value = ctx.measure(lambda: operation(overlay_model, direct=False, **kwargs))
    plain, plain_value = ctx.measure(lambda: operation(plain_model, direct=False, **kwargs))

    # The rewrite returns the same rows or it is not an optimisation. Checked
    # per cell rather than trusted from the test suite, because the tests run
    # on ten rows and this runs on a million -- and the failure mode the tests
    # cannot reach is a page boundary that only exists at scale.
    ctx.compare(f"{label} (rewritten vs direct)", direct_value, rewritten_value, "the rewrite changed the result")
    ctx.compare(f"{label} (overlay vs plain)", rewritten_value, plain_value)

    section.add(
        label,
        {"direct": direct, "rewritten": rewritten, "plain": plain},
        gain=harness.gain(direct, rewritten),
        rows=rows or ("capped" if rewritten_value is None else f"{rewritten_value:,}"),
    )


def _cursor(model):
    """A primary key about halfway through the view, for the keyset rows.

    Halfway rather than early: a cursor at the start excludes nothing, so it
    measures the unscoped page again under a different name. The interesting
    question is what a cursor is worth once it has started excluding rows.
    """
    keys = list(model.objects.order_by("id").values_list("pk", flat=True))
    return keys[len(keys) // 2] if keys else 0


def run(ctx):
    from tests.testapp.models import BenchPerson, PlainPerson

    models = (BenchPerson, PlainPerson)
    cursor = _cursor(BenchPerson)

    for attempt in range(1, ctx.passes + 1):
        suffix = f" (pass {attempt})" if ctx.passes > 1 else ""

        section = harness.Section(
            f"Scope breadth -- the axis the rewrite tracks{suffix}",
            COLUMNS,
            note="a page of 50, ordered by id, under filters of decreasing breadth",
        )
        row(ctx, section, "unscoped", page, models, scope=UNSCOPED)
        row(ctx, section, "~10% of the table", page, models, scope=SCOPE_10_PERCENT)
        row(ctx, section, "~1% of the table", page, models, scope=SCOPE_1_PERCENT)
        row(ctx, section, "~0.1% of the table", page, models, scope=SCOPE_TENTH_PERCENT)
        yield section

        section = harness.Section(
            f"Page size -- the axis it does not{suffix}",
            COLUMNS,
            note="unscoped; the saving is the scan that feeds the sort, not the rows returned",
        )
        for size in (20, 200, 2000):
            row(ctx, section, f"unscoped, page of {size:,}", page, models, scope=UNSCOPED, size=size)
        yield section

        section = harness.Section(
            f"What a paginator actually runs{suffix}",
            COLUMNS,
            note="OFFSET deepens; a cursor narrows. count() is what PageNumberPagination adds per page",
        )
        row(ctx, section, "deep offset (100,000)", page, models, scope=UNSCOPED, offset=100_000)
        row(ctx, section, "cursor, halfway in", keyset_page, models, scope=UNSCOPED, cursor=cursor)
        row(ctx, section, "cursor + ~1% scope", keyset_page, models, scope=SCOPE_1_PERCENT, cursor=cursor)
        row(ctx, section, "count(), unscoped", count, models, scope=UNSCOPED)
        yield section

        # Two shapes the rewrite declines, so both columns run the same query.
        # Whatever gap appears here is drift, and it is the floor every gain
        # above has to clear before it means anything. The m2m row is also the
        # one that would catch the rewrite firing where it must not: it
        # multiplies rows, and a rewrite that fetched by key would return fewer
        # of them -- which `ctx.compare` above turns into a disagreement rather
        # than a fast wrong answer.
        section = harness.Section(f"Declined -- the noise floor{suffix}", COLUMNS)
        row(ctx, section, "m2m page (multiplies rows)", page, models, scope={"addresses__city": "city0"})
        row(ctx, section, "unordered page", unordered_page, models, scope=UNSCOPED)
        yield section


def unordered_page(model, scope, direct, size=PAGE_SIZE):
    """No ordering, so the rewrite declines -- and so does any early exit.

    Kept separate from `page()` rather than folded in behind a flag, because
    the two differ in what they are allowed to return: an unordered LIMIT may
    return any rows at all, so this row's two columns are compared on count
    alone and would be wrong to compare on identity.
    """
    queryset = _arm(model.objects.filter(**scope), direct)
    return len(list(queryset[:size]))
