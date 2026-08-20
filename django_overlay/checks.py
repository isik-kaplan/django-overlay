from django.apps import apps
from django.core import checks
from django.db import connections, models

from .constraints import OverlayUniqueConstraint
from .fields import OverlayForeignKey
from .introspection import table_indexes
from .sync import resolve_schema
from .uniqueness import suggested_constraint


@checks.register(checks.Tags.models)
def check_no_plain_fk_to_overlay_models(app_configs, **kwargs):
    """Fails `manage.py check` for a plain ForeignKey/OneToOneField
    pointing at an OverlayModel, or a ManyToManyField pointing at one
    without an explicit through= model."""
    errors = []
    for model in apps.get_models(include_auto_created=True):
        for field in model._meta.get_fields():
            if not field.is_relation:
                continue
            related_model = field.related_model
            if related_model is None or not getattr(related_model, "_is_overlay_view_model", False):
                continue

            # M2M fields are never "concrete" (even the declaring side), so
            # check the type directly instead.
            if isinstance(field, models.ManyToManyField):
                if not field.remote_field.through._meta.auto_created:
                    continue
                errors.append(
                    checks.Error(
                        f"{model.__name__}.{field.name} is a plain ManyToManyField pointing at "
                        f"{related_model.__name__}, which is a django_overlay view model. Django's "
                        "auto-created through table would use a plain ForeignKey, which can't hold "
                        "a real FK constraint against a view.",
                        hint="Use django_overlay.fields.OverlayManyToManyField (through=... is "
                        "required) or write your own explicit through model with OverlayForeignKey "
                        "fields.",
                        obj=field,
                        id="django_overlay.E002",
                    )
                )
                continue

            # Every concrete, non-M2M relation field Django has is a
            # ForeignKey or OneToOneField (or a subclass), so this is
            # already effectively "is a plain FK/O2O".
            if not getattr(field, "concrete", False):
                continue
            if isinstance(field, OverlayForeignKey):
                continue
            errors.append(
                checks.Error(
                    f"{model.__name__}.{field.name} is a plain {type(field).__name__} pointing at "
                    f"{related_model.__name__}, which is a django_overlay view model. Postgres cannot "
                    f"hold a real FK constraint against a view.",
                    hint=f"Use django_overlay.fields.OverlayForeignKey instead of {type(field).__name__}.",
                    obj=field,
                    id="django_overlay.E001",
                )
            )
    return errors


@checks.register(checks.Tags.models)
def check_overlay_uniqueness(app_configs, **kwargs):
    """Fails for any uniqueness rule on an OverlayModel that isn't an
    OverlayUniqueConstraint.

    An overlay model is queried through a view spanning the base table and the
    source table, so uniqueness has to hold across both. Every other way Django
    lets you declare it compiles down to a single index or table constraint on
    the base table alone, which accepts a value that already exists in the
    source. See django_overlay/uniqueness.py."""
    errors = []
    for model in apps.get_models():
        if not getattr(model, "_is_overlay_view_model", False):
            continue
        problems = unsupported_uniqueness(model)
        if problems:
            errors.append(uniqueness_error(model, problems))
    return errors


def unsupported_uniqueness(model):
    """[(complaint, fields, name), ...] for every uniqueness rule on `model`
    that isn't an OverlayUniqueConstraint.

    Read off the built models rather than the class namespace: `unique_together`
    and `constraints` land on the hidden base model, while the declared fields
    (including a OneToOneField, which the base model stores as the ForeignKey
    underneath) stay on the view model. Collected in one pass rather than
    reported one at a time — several successive boot failures to fix a single
    model is a miserable way to learn a rule."""
    base_meta = model._base_model._meta
    problems = []

    for entry in base_meta.unique_together:
        fields = (entry,) if isinstance(entry, str) else tuple(entry)
        problems.append((f"Meta.unique_together = {list(fields)}", fields, None))

    covered = set()
    for constraint in base_meta.constraints:
        if isinstance(constraint, OverlayUniqueConstraint):
            covered.add(tuple(constraint.fields))
            continue
        if not isinstance(constraint, models.UniqueConstraint):
            continue
        detail = f"Meta.constraints has a plain UniqueConstraint {constraint.name!r}"
        if constraint.condition is not None:
            detail += " (with a condition)"
        problems.append((detail, tuple(constraint.fields), constraint.name))

    for field in model._meta.fields:
        if field.primary_key or not field.unique:
            continue
        if isinstance(field, models.OneToOneField):
            # Keeps working — it just needs its uniqueness spelled out, since
            # the implicit one covers the base table only.
            if (field.name,) not in covered:
                problems.append(
                    (
                        f"{field.name} is a OneToOneField, whose implicit uniqueness covers your table only",
                        (field.name,),
                        None,
                    )
                )
        else:
            problems.append((f"{field.name} declares unique=True", (field.name,), None))
    return problems


def uniqueness_error(model, problems):
    table = model._base_model._meta.db_table
    complaints = "\n".join(f"  - {complaint}" for complaint, _, _ in problems)
    suggestions = "\n".join(f"        {suggested_constraint(table, fields, name)}," for _, fields, name in problems)
    hint = (
        "Declare them as OverlayUniqueConstraint in Meta.constraints instead:\n\n"
        f"    constraints = [\n{suggestions}\n    ]\n\n"
        "Those names are the ones django_overlay would have generated; any name that's "
        "unique across your models will do."
    )
    if any("with a condition" in complaint for complaint, _, _ in problems):
        hint += (
            "\n\nConditional uniqueness isn't supported at all: the source-side trigger has no "
            "way to apply the condition, so it would check for collisions the condition should "
            "have excluded. If you genuinely want a condition over your own rows only, add the "
            "partial index by hand in a RunSQL migration and leave it out of Meta."
        )
    return checks.Error(
        f"{model.__name__} declares uniqueness django_overlay can't honour:\n\n{complaints}\n\n"
        "An overlay model is queried through a view spanning your table and the source table, "
        "so uniqueness has to hold across both. Every one of the above compiles down to a "
        "single index on your table alone, which would accept a value that already exists in "
        "the source. OverlayUniqueConstraint adds the source-side check.",
        hint=hint,
        obj=model,
        id="django_overlay.E003",
    )


@checks.register(checks.Tags.database)
def check_source_indexes_match(app_configs, databases=None, **kwargs):
    """Warns when a base table and its source table are indexed differently.

    The view is a `UNION ALL` over both, so a query is only as fast as its
    *slower* branch. An index on one side and not the other means half of every
    filtered query is a sequential scan — measured at 8x on a 3,000,000-row
    view, and completely silent: the query works and returns the right rows.

    Registered under Tags.database rather than Tags.models because it has to
    read the live schema. Django only runs database-tagged checks when asked
    (`manage.py check --database default`), so this never fires during
    makemigrations or on a machine with no database.

    Warnings rather than Errors throughout: the source table is shared and
    read-only, and the operator may have no DDL rights on it. Failing boot for
    something nobody can fix would be worse than the 8x."""
    return _for_each_comparable_model(databases, _index_parity_warning)


@checks.register(checks.Tags.database)
def check_source_indexes_cover_relations(app_configs, databases=None, **kwargs):
    """Warns when a relation or uniqueness column has no index on the *source*.

    Distinct from the parity check above, and it catches two things that one
    cannot:

    * **relation columns.** Django sets `db_index=True` on every ForeignKey, so
      the base table gets one whether you asked or not — the vendor's table
      does not. Parity catches that only because the base happens to have it;
      this states the requirement directly.
    * **uniqueness columns.** An OverlayUniqueConstraint's trigger runs
      `SELECT 1 FROM <source> WHERE col = NEW.col` on **every insert**. With no
      index on the source that is a sequential scan of the vendor table per
      row, and parity will not notice, because the base side's index is a
      *partial unique* one whose shape matches nothing on the source.

    Many-to-many needs no special handling: the through model is a model like
    any other, so if it is an overlay model with a source it is checked here on
    its own account, and if it is plain there is no source to index."""
    return _for_each_comparable_model(databases, _uncovered_columns_warning)


def _for_each_comparable_model(databases, build_message):
    if not databases:
        return []
    messages = []
    for alias in databases:
        connection = connections[alias]
        with connection.cursor() as cursor:
            for model in _overlay_models_with_a_source():
                tables = _comparable_tables(cursor, connection, model)
                if tables is None:
                    continue
                message = build_message(cursor, model, *tables)
                if message is not None:
                    messages.append(message)
    return messages


def _overlay_models_with_a_source():
    return [
        model
        for model in apps.get_models()
        if getattr(model, "_is_overlay_view_model", False) and model.get_source() is not None
    ]


def _comparable_tables(cursor, connection, model):
    """(schema, base_table, source), or None when either table is missing.

    Missing means migrations have not run, or the vendor table is not there
    yet. Neither is this check's business to complain about."""
    source = model.get_source()
    base_table = model._base_model._meta.db_table
    schema = resolve_schema(connection)
    if not (_table_exists(cursor, source.schema, source.table) and _table_exists(cursor, schema, base_table)):
        return None
    return schema, base_table, source


def _table_exists(cursor, schema: str, table: str) -> bool:
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [f'"{schema}"."{table}"'])
    return cursor.fetchone()[0]


def _index_columns(shape: str) -> list[str]:
    """['company_id', 'created_at'] from 'btree (company_id, created_at)'.

    Parenthesis-matched rather than split on the last ')': a partial index's
    shape is `btree (ssn) WHERE (NOT _overlay_deleted)`, and taking the final
    parenthesis swallows the predicate into the column list. Expression
    indexes (`btree ((-id))`) come back as a single opaque entry, which is
    exactly what is wanted — two of them match only if they are the same
    expression."""
    if "(" not in shape:
        return []
    start = shape.index("(")
    depth, inner = 0, None
    for position, character in enumerate(shape[start:], start):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                inner = shape[start + 1 : position]
                break
    if inner is None:
        return []

    columns, level, current = [], 0, ""
    for character in inner:
        if character == "," and level == 0:
            columns.append(current)
            current = ""
            continue
        if character == "(":
            level += 1
        elif character == ")":
            level -= 1
        current += character
    columns.append(current)
    return [column.strip().strip('"') for column in columns if column.strip()]


def _ignorable(index, pk_column: str) -> bool:
    """Indexes with no meaningful counterpart on the other side: the primary
    key itself, and any index *on* soft delete's base-only shadow flag.

    Tested against the index's columns, not against the whole shape string. A
    uniqueness index is usually *filtered by* `_overlay_deleted`
    (`btree (ssn) WHERE NOT _overlay_deleted`) while being an index on `ssn`,
    and treating that as ignorable would silently drop every uniqueness index
    from the comparison."""
    columns = _index_columns(index["shape"])
    return columns == [pk_column] or "_overlay_deleted" in columns


def _covered_column_sets(indexes, pk_column):
    """{('city',), ('a', 'b'), ...} for the indexes worth requiring a
    counterpart for.

    Compared on **columns**, not on `compare_indexes`' shape strings. Shape
    equality is right for `show_source_indexes`, which reports to a human, but
    too strict here: the base table's uniqueness index is unique and often
    partial (`btree (ssn) WHERE NOT _overlay_deleted`) while the source only
    ever needs a plain `btree (ssn)`. Comparing shapes would demand the source
    grow a unique index it must not have, and then complain forever."""
    sets = set()
    for index in indexes:
        if _ignorable(index, pk_column):
            continue
        columns = _index_columns(index["shape"])
        if columns:
            sets.add(tuple(columns))
    return sets


def _index_parity_warning(cursor, model, schema, base_table, source):
    pk_column = model._meta.pk.column
    theirs = _covered_column_sets(table_indexes(cursor, source.schema, source.table), pk_column)
    ours = _covered_column_sets(table_indexes(cursor, schema, base_table), pk_column)

    absent_there = sorted(ours - theirs)
    absent_here = sorted(theirs - ours)
    if not absent_there and not absent_here:
        return None

    lines = []
    if absent_there:
        lines.append(f"  on {base_table} but not on {source.schema}.{source.table}:")
        lines += [f"    - ({', '.join(columns)})" for columns in absent_there]
    if absent_here:
        lines.append(f"  on {source.schema}.{source.table} but not on {base_table}:")
        lines += [f"    - ({', '.join(columns)})" for columns in absent_here]

    return checks.Warning(
        f"{model.__name__} is indexed differently from its source table:\n\n" + "\n".join(lines) + "\n\n"
        "The view reads both tables, so a filter is only as fast as the branch without "
        "the index — the other one falls back to a sequential scan.",
        hint=(
            "Run `manage.py show_source_indexes` for the full comparison, then add the "
            "missing indexes to whichever side is short. Note that Django indexes every "
            "ForeignKey column automatically, so your table can have indexes you never "
            "declared. If the source table isn't yours to change, silence this with "
            "SILENCED_SYSTEM_CHECKS."
        ),
        obj=model,
        id="django_overlay.W001",
    )


def _columns_needing_a_source_index(model) -> dict:
    """{column: why} for every column whose lookups hit the source table."""
    needed = {}
    for field in model._meta.concrete_fields:
        if field.is_relation and field.column != model._meta.pk.column:
            kind = "one-to-one" if field.one_to_one else "foreign key"
            needed[field.column] = f"{field.name} is a {kind}, so joins and reverse lookups read the source"
    # Constraints live on the hidden base model — see the note above
    # _BASE_ONLY_META_OPTIONS in models.py — and get_constraints() is an
    # instance method, so read them the same way check_overlay_uniqueness does.
    for constraint in model._base_model._meta.constraints:
        if isinstance(constraint, OverlayUniqueConstraint):
            # The trigger looks up all the constrained columns together, so one
            # index leading with the first of them serves it. Requiring an
            # index per column would ask for indexes that buy nothing.
            leading = model._meta.get_field(constraint.fields[0]).column
            needed.setdefault(leading, f"{constraint.name} checks the source for a duplicate on every insert")
    return needed


def _uncovered_columns_warning(cursor, model, schema, base_table, source):
    needed = _columns_needing_a_source_index(model)
    if not needed:
        return None
    leading = {
        columns[0]
        for columns in map(_index_columns, (i["shape"] for i in table_indexes(cursor, source.schema, source.table)))
        if columns
    }
    missing = {column: why for column, why in needed.items() if column not in leading}
    if not missing:
        return None

    lines = [f"    - {column}: {why}" for column, why in sorted(missing.items())]
    return checks.Warning(
        f"{model.__name__} has columns with no index on {source.schema}.{source.table}:\n\n"
        + "\n".join(lines)
        + "\n\nDjango indexes these on your table automatically; nothing does it on the vendor's.",
        hint=(
            f"Add a btree index on each of {sorted(missing)} to "
            f"{source.schema}.{source.table}. If the source table isn't yours to change, "
            "silence this with SILENCED_SYSTEM_CHECKS."
        ),
        obj=model,
        id="django_overlay.W002",
    )
