"""Uniqueness on an overlay model.

An overlay model is queried through a view spanning your base table and the
source table, so uniqueness has to hold across both — anything less isn't
uniqueness as far as application code can tell. Every way Django has of
declaring it (`Meta.unique_together`, `Field(unique=True)`, `OneToOneField`,
plain `UniqueConstraint`) compiles down to one index or table constraint on
your base table alone, which happily accepts a value that already exists in the
source. `OverlayUniqueConstraint` is the only form that also gets the
source-side trigger, so it's the only form allowed; everything else is rejected
with the line to write instead. That rejection lives in `checks.py`, with the
package's other model-misconfiguration rules — this module owns the naming and
the narrowing.

A `OneToOneField` is the one exception, because rejecting it would cost real
ORM behaviour (the singular reverse accessor) for a purely mechanical reason.
It keeps working: the base model quietly gets the plain ForeignKey underneath
so no table constraint is emitted (see fields.base_model_copy), and the model
is required to declare the matching `OverlayUniqueConstraint` itself.

Requiring it even on a source-less model is deliberate. It costs nothing there
(no source means no trigger, just the plain index) and it means a model that
later gains a source is already correct.

Second job, for `soft_delete` models only: a soft-deleted row stays in the base
table as a hidden tombstone, so its unique value would stay reserved forever
and `full_clean()` — which queries the view, where the tombstone is invisible —
would pass values the insert then rejects. Every constraint on such a model is
narrowed to `WHERE NOT _overlay_deleted`. Postgres only accepts a predicate on
an index, which is a second reason table constraints can't be used.

The primary key is the one rule that can't be narrowed and can't be moved — a
partial primary key isn't a thing. In practice that only affects code assigning
explicit pks: organic rows take theirs from a sequence and source-backed rows
keep the identity they had, so neither reuses one.
"""

import hashlib

from django.db.models.expressions import Col
from django.db.models.sql import Query

from .constraints import OverlayUniqueConstraint


MAX_IDENTIFIER_LENGTH = 63


def constraint_name(table: str, fields) -> str:
    """Stable, collision-resistant, and short enough for Postgres. Stability
    matters most: a name that moved between runs would churn migrations."""
    name = f"{table}_{'_'.join(fields)}_uniq"
    if len(name) <= MAX_IDENTIFIER_LENGTH:
        return name
    digest = hashlib.md5(name.encode()).hexdigest()[:8]  # noqa: S324 - naming, not security
    return f"{name[: MAX_IDENTIFIER_LENGTH - len(digest) - 1]}_{digest}"


def trigger_name(base_table: str, constraint_name: str) -> str:
    """The name of the source-side trigger backing one OverlayUniqueConstraint.

    Shared rather than derived where it is needed, because it is needed in
    three places now: the migration operation that creates the trigger, the one
    that drops it, and the resync that rebuilds it when a model's source
    changes. Two of those computing the name independently is how a trigger
    ends up orphaned under a name nothing drops -- still firing, still probing
    the table it was written against."""
    return f"overlayunique_{base_table}_{constraint_name}"[:MAX_IDENTIFIER_LENGTH]


def suggested_constraint(table: str, fields, name: str | None = None) -> str:
    """The line to paste into Meta.constraints. No condition= in it — a
    soft_delete model's constraints are narrowed for you, so the caller never
    has to name `_overlay_deleted`, a base-only column they can't see."""
    field_list = ", ".join(f'"{field}"' for field in fields)
    return f'OverlayUniqueConstraint(fields=[{field_list}], name="{name or constraint_name(table, fields)}")'


def _narrow(constraint):
    """A copy of `constraint` that ignores tombstoned rows. Only
    OverlayUniqueConstraints need it — check() has already established that
    nothing else in the list is a uniqueness rule."""
    if isinstance(constraint, OverlayUniqueConstraint):
        path, args, kwargs = constraint.deconstruct()
        return type(constraint)(*args, **{**kwargs, "soft_delete": True})
    return constraint


def narrow_for_soft_delete(base_options: dict) -> dict:
    """The base model's Meta options with every constraint narrowed to ignore
    tombstoned rows. Reversible — see for_validation()."""
    options = dict(base_options)
    declared = options.get("constraints") or []
    if declared:
        options["constraints"] = [_narrow(constraint) for constraint in declared]
    return options


def for_validation(constraints):
    """The same constraints with the tombstone predicate taken back off.

    Validation needs them un-narrowed: `_overlay_deleted` is base-only, so
    `Q(_overlay_deleted=False)` doesn't resolve on the view model — and the
    view already hides tombstoned rows, so the predicate would be redundant
    even if it did. Derived on demand rather than stashed alongside the real
    list, so there's only ever one source of truth to keep in step."""
    return [
        constraint.without_soft_delete_narrowing() if isinstance(constraint, OverlayUniqueConstraint) else constraint
        for constraint in constraints
    ]


def condition_columns(constraint, model):
    """The columns `constraint`'s own condition reads, first-seen order.

    Built against `model` the way Django builds an index's predicate, with
    joins refused, so a lookup across a relation raises FieldError here, as
    does a field `model` lacks."""
    if constraint.own_condition is None:
        return []
    where = Query(model=model, alias_cols=False).build_where(constraint.own_condition)
    return list(dict.fromkeys(col.target.column for col in _cols(where)))


def _cols(node):
    if isinstance(node, Col):
        yield node
        return
    # filter: an expression may have an empty slot, a Case without a default.
    for child in filter(None, node.get_source_expressions()):
        yield from _cols(child)


def condition_sql(constraint, model, schema_editor):
    """`constraint`'s own condition as SQL over unqualified column names, its
    values inlined, ready for a trigger template: what Django writes in the
    partial index's WHERE, minus the tombstone half.

    Unqualified on purpose. The trigger evaluates it twice, once over the new
    row and once inside the probe of the source table, and the bare names
    resolve to whichever row is in scope. That holds because E007 keeps it to
    the model's own columns, which both have.

    Percent signs come back doubled: trigger SQL is run with empty params, so
    psycopg reads one as a placeholder (see sync.statement_executor)."""
    if constraint.own_condition is None:
        return None
    connection = schema_editor.connection
    query = Query(model=model, alias_cols=False)
    where = query.build_where(constraint.own_condition)
    sql, params = where.as_sql(query.get_compiler(connection=connection), connection)
    return (sql % tuple(schema_editor.quote_value(p) for p in params)).replace("%", "%%")
