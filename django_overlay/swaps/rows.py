"""Whether the rows that already exist survive the swap.

Each one is an existing trigger's predicate, transposed. The triggers ask
"is this row being written valid"; a swap writes nothing they can see, so the
same question has to be asked with the quantifier moved -- "are all the rows
that already exist still valid against the table we are about to point at".

These are also the checks a concurrent write can invalidate, which is why the
cutover re-runs exactly this set while holding the lock.
"""

from ..sync import inbound_overlay_foreign_keys, overlay_unique_constraints
from .probes import _Probe
from .report import ERROR, WARNING, Finding, _qualified


def _check_identity(probe: _Probe) -> list[Finding]:
    """Whether the candidate means the same thing by an id as the current
    source does. Nothing else in this module matters as much."""
    if not probe.identity_columns:
        return [
            Finding(
                "S005",
                WARNING,
                "No identity_columns given, so nothing verified that the candidate means the same "
                "entity by an id as the current source does. Renumbering is the one failure here "
                "that raises nothing, breaks nothing visibly, and silently repoints every override, "
                "tombstone and foreign key at a different row. Pass the source's natural key.",
            )
        ]
    reassigned, renumbered = probe.scalar_row(
        "identity_drift.sql.j2",
        current=probe.current,
        candidate=probe.candidate,
        identity_columns=list(probe.identity_columns),
    )
    findings = []
    if reassigned:
        findings.append(
            Finding(
                "S003",
                ERROR,
                f"{reassigned:,} id(s) carry a different {list(probe.identity_columns)} in "
                f"{_qualified(probe.candidate)} than in {_qualified(probe.current)}. Every override, "
                "tombstone and foreign key holding one of those now points at a different entity, "
                "and nothing will raise.",
            )
        )
    if renumbered:
        findings.append(
            Finding(
                "S004",
                ERROR,
                f"{renumbered:,} row(s) present in both tables changed id. References to them "
                "dangle and their overrides no longer shadow them.",
            )
        )
    return findings


def _check_orphaned_base_rows(probe: _Probe) -> list[Finding]:
    orphaned = probe.count(
        "orphaned_base_rows.sql.j2",
        tenant_schema=probe.tenant_schema,
        base_table=probe.base_table,
        pk_column=probe.pk_column,
        negate=probe.negate,
        current=probe.current,
        candidate=probe.candidate,
    )
    if not orphaned:
        return []
    return [
        Finding(
            "S006",
            WARNING,
            f"{orphaned:,} base row(s) are backed by a source row the candidate does not have. "
            "They keep their values and stay visible — materialisation copies the whole row — but "
            "they stop being vendor-backed: source_row() returns None and reset_to_source() has "
            "nothing to reset to.",
        )
    ]


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


# Everything about the rows themselves -- each one a trigger's predicate run
# backwards over the data that already exists. These are the checks a
# concurrent write can invalidate, which is why the cutover re-runs exactly
# this tuple while holding the lock.
ROW_CHECKS = (
    _check_identity,
    _check_orphaned_base_rows,
    _check_dangling_references,
    _check_uniqueness,
)
