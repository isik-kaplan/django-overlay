"""Whether an id still means what it meant.

The source's id is the overlay's identity -- the view's primary key, the value
a materialised row shadows, the value a tombstone masks, the value in every
OverlayForeignKey column pointing here. Renumbering is the failure this whole
package exists for: it raises nothing, breaks nothing visibly, and leaves
every one of those resolving perfectly, to the wrong row.

The second check is the same question from the other side -- base rows whose
source row the candidate no longer has. That one only warns, because a
materialised row keeps its values and stays visible; what it loses is its
backing.
"""

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
