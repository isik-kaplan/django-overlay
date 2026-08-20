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
