"""The constraint triggers' predicates, run backwards over the rows that
already exist.

An OverlayForeignKey and an OverlayUniqueConstraint are each enforced by a
trigger that asks, of one row being written, whether it is valid. A swap
writes no row any of them can see, so the same question has to be asked with
the quantifier moved: not "is this reference valid" but "are all the
references that already exist still valid against the table we are about to
point at".

Both checks count against the current source as well as the candidate, so a
problem that is already true today is reported as the pre-existing one it is
rather than blamed on the swap. Only what the swap *creates* blocks.
"""

from ..sync import inbound_overlay_foreign_keys, overlay_unique_constraints
from .probes import _Probe
from .report import ERROR, WARNING, Finding, _qualified


def _check_dangling_references(probe: _Probe) -> list[Finding]:
    """Every OverlayForeignKey pointing at this model, checked against the
    candidate the way its trigger would check one row.

    A count against the current source comes with it, so a reference that is
    already dangling today is reported as the pre-existing problem it is rather
    than blamed on the swap. Only references the swap *creates* block."""
    findings = []
    for referencing, field in inbound_overlay_foreign_keys(probe.model):
        context = {
            "tenant_schema": probe.tenant_schema,
            "referencing_table": referencing._meta.db_table,
            "column": field.column,
            "base_table": probe.base_table,
            "target_pk": probe.pk_column,
            "target_soft_delete": probe.soft_delete,
            "negate": probe.negate,
        }
        after = probe.count("dangling_references.sql.j2", source=probe.candidate, **context)
        if not after:
            continue
        before = probe.count("dangling_references.sql.j2", source=probe.current, **context)
        label = f"{referencing._meta.db_table}.{field.column}"
        if after > before:
            findings.append(
                Finding(
                    "S007",
                    ERROR,
                    f"{after - before:,} reference(s) in {label} point at a row the candidate does "
                    f"not make visible ({before:,} of {after:,} already dangle today).",
                )
            )
        else:
            findings.append(
                Finding(
                    "S007",
                    WARNING,
                    f"{after:,} reference(s) in {label} already dangle today and still would. "
                    "The swap does not cause this.",
                )
            )
    return findings


def _check_uniqueness(probe: _Probe) -> list[Finding]:
    """Each OverlayUniqueConstraint, checked in both of the ways a swap can
    break it: a candidate that holds a value some base row already holds, and a
    candidate that holds one twice itself."""
    findings = []
    for constraint in overlay_unique_constraints(probe.model):
        columns = [probe.base_model._meta.get_field(name).column for name in constraint.fields]
        collisions = probe.count(
            "source_base_collisions.sql.j2",
            tenant_schema=probe.tenant_schema,
            base_table=probe.base_table,
            columns=columns,
            pk_column=probe.pk_column,
            negate=probe.negate,
            soft_delete=probe.soft_delete,
            source=probe.candidate,
        )
        if collisions:
            findings.append(
                Finding(
                    "S009",
                    ERROR,
                    f"{constraint.name}: {collisions:,} row(s) in {_qualified(probe.candidate)} hold a "
                    f"{columns} that a base row already holds. The constraint would be violated the "
                    "moment the view reads both, and no index or trigger raises for it.",
                )
            )

        after = probe.count("duplicate_values.sql.j2", columns=columns, source=probe.candidate)
        if not after:
            continue
        before = probe.count("duplicate_values.sql.j2", columns=columns, source=probe.current)
        if after > before:
            findings.append(
                Finding(
                    "S008",
                    ERROR,
                    f"{constraint.name}: {after:,} {columns} value(s) appear more than once within "
                    f"{_qualified(probe.candidate)} ({before:,} do today).",
                )
            )
        else:
            findings.append(
                Finding(
                    "S008",
                    WARNING,
                    f"{constraint.name}: {after:,} {columns} value(s) already appear more than once "
                    "within the current source and still would. Nothing in this package has ever "
                    "enforced uniqueness within the source itself.",
                )
            )
    return findings
