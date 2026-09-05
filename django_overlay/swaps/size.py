"""Roughly how much is in the candidate, and whether anyone analysed it.

The one check here is against the load rather than against the schema: a
candidate a job half-filled has exactly the shape of one that filled
completely, and differs only in how much is in it. It is also the check most
easily read as noise, so it is worth being clear about which half blocks -- an
empty candidate is a load that did not happen, a small one is a load that may
have been meant.
"""

from .probes import _estimated_rows, _Probe
from .report import ERROR, WARNING, Finding, _qualified


def _check_row_estimate(probe: _Probe) -> list[Finding]:
    """A truncated or half-loaded candidate, and a candidate nobody analysed.

    Estimates, deliberately: this is a sanity check against a load that went
    wrong, and count(*) on the table this package exists to sit in front of is
    not something to run while holding a lock."""
    findings = []
    current_rows, _ = _estimated_rows(probe.cursor, probe.current)
    candidate_rows, lowest = _estimated_rows(probe.cursor, probe.candidate)

    if lowest < 0:
        findings.append(
            Finding(
                "S015",
                WARNING,
                f"{_qualified(probe.candidate)} has never been analysed, so the planner has no "
                "statistics for it. The view is a UNION ALL, which is already the shape Postgres "
                "estimates worst — run ANALYZE before the cutover, not after.",
            )
        )
    elif candidate_rows == 0:
        findings.append(Finding("S013", ERROR, f"{_qualified(probe.candidate)} is empty."))
    elif current_rows and candidate_rows < current_rows * probe.min_row_ratio:
        findings.append(
            Finding(
                "S013",
                WARNING,
                f"{_qualified(probe.candidate)} holds about {candidate_rows:,} rows against "
                f"{current_rows:,} today ({candidate_rows / current_rows:.0%}).",
            )
        )
    return findings
