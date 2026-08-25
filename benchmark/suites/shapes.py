"""Overlay view against plain mirror, shape by shape, in raw SQL.

The cheapest suite and the broadest: point lookups, ordered pages, counts and
cross-boundary joins, each written twice -- once against the view and once
against a plain table holding identical rows with identical indexes.

It is raw SQL rather than the ORM on purpose. Everything else here measures
what Django emits, which folds the ORM's choices into the number; this measures
the view itself. When a ratio moves in both suites the view changed, and when
it moves in only one the ORM did.

Two view shapes are compared throughout, because the library offers both and
they cost very differently:

    entities   overridable + soft_delete  -> full anti-join plus a qual
    links      neither                    -> a bare UNION ALL

The plan column is the shorthand from `graph.shape_of` -- whether Postgres
reached for a Merge Append, a sort, or a nested loop with a materialise under
it.
"""

from benchmark import graph, harness


NAME = "shapes"
TITLE = "The view against a plain mirror, shape by shape"

COLUMNS = ("overlay", "plain", "ratio", "plan", "notes")


def _report(ctx, section, label, view_sql, plain_sql, notes=""):
    view_ms, view_timeout = graph.best_of(view_sql)
    plain_ms, _ = graph.best_of(plain_sql)
    # best_of clears the session timeout on its way out, and EXPLAIN ANALYZE
    # runs the query for real -- so without this the plan below is the one
    # uncapped statement in the suite.
    harness.set_statement_cap(ctx.cap_ms)
    shape = "capped" if view_timeout else graph.shape_of(graph.plan(view_sql))
    ratio = f"x{view_ms / plain_ms:.2f}" if plain_ms and not view_timeout else ""
    section.add(
        label,
        {"overlay": harness.Cell(view_ms, capped=view_timeout), "plain": harness.Cell(plain_ms)},
        ratio=ratio,
        plan=shape,
        notes=notes,
    )


def _relation_sizes():
    section = harness.Section(
        "What was loaded",
        ("view", "base", "source", "shape"),
    )
    for name, (base, source, _) in (graph.ENTITIES | graph.LINKS).items():
        kind = "full anti-join + qual" if name in graph.ENTITIES else "bare UNION ALL"
        section.add(
            name,
            view=f"{graph.scalar(f'SELECT count(*) FROM {base}_view'):,}",
            base=f"{graph.scalar(f'SELECT count(*) FROM {base}'):,}",
            source=f"{graph.scalar(f'SELECT count(*) FROM {source}'):,}",
            shape=kind,
        )
    return section


def run(ctx):
    plain = graph.PLAIN
    yield _relation_sizes()

    section = harness.Section("A. Entity shape (overridable + soft_delete) -- the expensive half", COLUMNS)
    _report(
        ctx,
        section,
        "point lookup by pk",
        "SELECT * FROM bench_person_view WHERE id = (SELECT id FROM bench_person_view LIMIT 1)",
        f"SELECT * FROM {plain['person']} WHERE id = (SELECT id FROM {plain['person']} LIMIT 1)",
    )
    _report(
        ctx,
        section,
        "equality on indexed column (city)",
        "SELECT * FROM bench_person_view WHERE city = 'city42'",
        f"SELECT * FROM {plain['person']} WHERE city = 'city42'",
    )
    _report(
        ctx,
        section,
        "equality on UNindexed column (born_on)",
        "SELECT * FROM bench_person_view WHERE born_on = DATE '1970-01-01'",
        f"SELECT * FROM {plain['person']} WHERE born_on = DATE '1970-01-01'",
    )
    _report(
        ctx,
        section,
        "scoped + ordered (city, score DESC)",
        "SELECT * FROM bench_person_view WHERE city = 'city42' ORDER BY score DESC LIMIT 20",
        f"SELECT * FROM {plain['person']} WHERE city = 'city42' ORDER BY score DESC LIMIT 20",
    )
    _report(
        ctx,
        section,
        "UNSCOPED ordered page",
        "SELECT * FROM bench_person_view ORDER BY score DESC LIMIT 20",
        f"SELECT * FROM {plain['person']} ORDER BY score DESC LIMIT 20",
    )
    _report(
        ctx,
        section,
        "UNSCOPED ordered, deep offset",
        "SELECT * FROM bench_person_view ORDER BY score DESC LIMIT 20 OFFSET 100000",
        f"SELECT * FROM {plain['person']} ORDER BY score DESC LIMIT 20 OFFSET 100000",
    )
    _report(
        ctx, section, "count(*)", "SELECT count(*) FROM bench_person_view", f"SELECT count(*) FROM {plain['person']}"
    )
    yield section

    section = harness.Section("B. Link shape (overridable=False, soft_delete=False) -- the cheap half", COLUMNS)
    _report(
        ctx,
        section,
        "person_id lookup",
        "SELECT * FROM bench_person_address_view WHERE person_id = (SELECT id FROM bench_person_view LIMIT 1)",
        f"SELECT * FROM {plain['person_address']} WHERE person_id = (SELECT id FROM {plain['person']} LIMIT 1)",
    )
    _report(
        ctx,
        section,
        "UNSCOPED ordered page",
        "SELECT * FROM bench_person_address_view ORDER BY person_id LIMIT 20",
        f"SELECT * FROM {plain['person_address']} ORDER BY person_id LIMIT 20",
    )
    _report(
        ctx,
        section,
        "UNSCOPED ordered, deep offset",
        "SELECT * FROM bench_person_address_view ORDER BY person_id LIMIT 20 OFFSET 100000",
        f"SELECT * FROM {plain['person_address']} ORDER BY person_id LIMIT 20 OFFSET 100000",
    )
    _report(
        ctx,
        section,
        "count(*)",
        "SELECT count(*) FROM bench_person_address_view",
        f"SELECT count(*) FROM {plain['person_address']}",
    )
    yield section

    one_person = "(SELECT id FROM bench_person_view LIMIT 1)"
    one_plain = f"(SELECT id FROM {plain['person']} LIMIT 1)"

    section = harness.Section("C. Where the two meet -- joins across the shape boundary", COLUMNS)
    _report(
        ctx,
        section,
        "detail: one person -> addresses",
        "SELECT a.* FROM bench_address_view a "
        "JOIN bench_person_address_view l ON l.address_id = a.id "
        f"WHERE l.person_id = {one_person}",
        f"SELECT a.* FROM {plain['address']} a "
        f"JOIN {plain['person_address']} l ON l.address_id = a.id "
        f"WHERE l.person_id = {one_plain}",
    )
    _report(
        ctx,
        section,
        "detail: one person -> all three relations",
        "SELECT a.id, p.id, e.id FROM bench_person_view person "
        "LEFT JOIN bench_person_address_view la ON la.person_id = person.id "
        "LEFT JOIN bench_address_view a ON a.id = la.address_id "
        "LEFT JOIN bench_person_phone_view lp ON lp.person_id = person.id "
        "LEFT JOIN bench_phone_view p ON p.id = lp.phone_id "
        "LEFT JOIN bench_person_email_view le ON le.person_id = person.id "
        "LEFT JOIN bench_email_view e ON e.id = le.email_id "
        f"WHERE person.id = {one_person}",
        f"SELECT a.id, p.id, e.id FROM {plain['person']} person "
        f"LEFT JOIN {plain['person_address']} la ON la.person_id = person.id "
        f"LEFT JOIN {plain['address']} a ON a.id = la.address_id "
        f"LEFT JOIN {plain['person_phone']} lp ON lp.person_id = person.id "
        f"LEFT JOIN {plain['phone']} p ON p.id = lp.phone_id "
        f"LEFT JOIN {plain['person_email']} le ON le.person_id = person.id "
        f"LEFT JOIN {plain['email']} e ON e.id = le.email_id "
        f"WHERE person.id = {one_plain}",
    )
    _report(
        ctx,
        section,
        "reverse: people at addresses in a city",
        "SELECT DISTINCT person.id FROM bench_person_view person "
        "JOIN bench_person_address_view l ON l.person_id = person.id "
        "JOIN bench_address_view a ON a.id = l.address_id "
        "WHERE a.city = 'city42' LIMIT 50",
        f"SELECT DISTINCT person.id FROM {plain['person']} person "
        f"JOIN {plain['person_address']} l ON l.person_id = person.id "
        f"JOIN {plain['address']} a ON a.id = l.address_id "
        f"WHERE a.city = 'city42' LIMIT 50",
    )
    _report(
        ctx,
        section,
        "reverse, rewritten as = ANY (ARRAY ...)",
        "SELECT person.id FROM bench_person_view person WHERE person.id = ANY (ARRAY("
        "SELECT l.person_id FROM bench_person_address_view l WHERE l.address_id = ANY (ARRAY("
        "SELECT a.id FROM bench_address_view a WHERE a.city = 'city42')))) LIMIT 50",
        f"SELECT person.id FROM {plain['person']} person WHERE person.id = ANY (ARRAY("
        f"SELECT l.person_id FROM {plain['person_address']} l WHERE l.address_id = ANY (ARRAY("
        f"SELECT a.id FROM {plain['address']} a WHERE a.city = 'city42')))) LIMIT 50",
    )
    _report(
        ctx,
        section,
        "reverse: people with a phone number",
        "SELECT person.id FROM bench_person_view person "
        "JOIN bench_person_phone_view l ON l.person_id = person.id "
        "JOIN bench_phone_view p ON p.id = l.phone_id "
        "WHERE p.number = '+447000000042' LIMIT 50",
        f"SELECT person.id FROM {plain['person']} person "
        f"JOIN {plain['person_phone']} l ON l.person_id = person.id "
        f"JOIN {plain['phone']} p ON p.id = l.phone_id "
        f"WHERE p.number = '+447000000042' LIMIT 50",
    )
    _report(
        ctx,
        section,
        "list page: 20 people + their address count",
        "SELECT person.id, count(l.id) FROM bench_person_view person "
        "LEFT JOIN bench_person_address_view l ON l.person_id = person.id "
        "WHERE person.city = 'city42' GROUP BY person.id ORDER BY person.id LIMIT 20",
        f"SELECT person.id, count(l.id) FROM {plain['person']} person "
        f"LEFT JOIN {plain['person_address']} l ON l.person_id = person.id "
        f"WHERE person.city = 'city42' GROUP BY person.id ORDER BY person.id LIMIT 20",
    )
    yield section

    # `person.addresses.all()` is the single most common query this schema will
    # serve. OverlayQuery's rewrite covers forward FK traversals, not M2M ones,
    # so this is whatever the ORM would emit -- written by hand to isolate it.
    section = harness.Section("D. The detail page, three ways -- can the gap be written away?", COLUMNS)
    _report(
        ctx,
        section,
        "1. JOIN through the link view",
        "SELECT a.* FROM bench_address_view a "
        "JOIN bench_person_address_view l ON l.address_id = a.id "
        f"WHERE l.person_id = {one_person}",
        f"SELECT a.* FROM {plain['address']} a "
        f"JOIN {plain['person_address']} l ON l.address_id = a.id "
        f"WHERE l.person_id = {one_plain}",
    )
    _report(
        ctx,
        section,
        "2. = ANY (ARRAY (subquery))",
        "SELECT a.* FROM bench_address_view a WHERE a.id = ANY (ARRAY("
        "SELECT l.address_id FROM bench_person_address_view l "
        f"WHERE l.person_id = {one_person}))",
        f"SELECT a.* FROM {plain['address']} a WHERE a.id = ANY (ARRAY("
        f"SELECT l.address_id FROM {plain['person_address']} l "
        f"WHERE l.person_id = {one_plain}))",
    )

    # What prefetch_related actually does: fetch the link rows, then fetch the
    # targets by a literal list of ids. No join, no correlated subquery.
    link_ids = [
        str(row[0])
        for row in graph.rows(f"SELECT address_id FROM bench_person_address_view WHERE person_id = {one_person}")
    ] or ["00000000-0000-7000-8000-000000000000"]
    literals = ", ".join(f"'{value}'::uuid" for value in link_ids)
    _report(
        ctx,
        section,
        "3. two queries, literal id list (prefetch_related)",
        f"SELECT * FROM bench_address_view WHERE id IN ({literals})",
        f"SELECT * FROM {plain['address']} WHERE id IN ({literals})",
        notes=f"{len(link_ids)} ids",
    )
    _report(
        ctx,
        section,
        "phone lookup, JOIN form",
        "SELECT person.id FROM bench_person_view person "
        "JOIN bench_person_phone_view l ON l.person_id = person.id "
        "JOIN bench_phone_view p ON p.id = l.phone_id "
        "WHERE p.number = '+447000000042' LIMIT 50",
        f"SELECT person.id FROM {plain['person']} person "
        f"JOIN {plain['person_phone']} l ON l.person_id = person.id "
        f"JOIN {plain['phone']} p ON p.id = l.phone_id "
        f"WHERE p.number = '+447000000042' LIMIT 50",
    )
    _report(
        ctx,
        section,
        "phone lookup, = ANY (ARRAY ...)",
        "SELECT person.id FROM bench_person_view person WHERE person.id = ANY (ARRAY("
        "SELECT l.person_id FROM bench_person_phone_view l WHERE l.phone_id = ANY (ARRAY("
        "SELECT p.id FROM bench_phone_view p WHERE p.number = '+447000000042')))) LIMIT 50",
        f"SELECT person.id FROM {plain['person']} person WHERE person.id = ANY (ARRAY("
        f"SELECT l.person_id FROM {plain['person_phone']} l WHERE l.phone_id = ANY (ARRAY("
        f"SELECT p.id FROM {plain['phone']} p WHERE p.number = '+447000000042')))) LIMIT 50",
    )
    yield section
