"""What a preflight found, and how a caller accepts part of it.

No database and no model state: a Finding is a code, a level and a sentence,
and a SwapReport is a list of them with a verdict. Kept apart from the checks
that produce them because it is the one piece of this that a caller handles
directly -- `report.ok`, `report.errors`, `str(report)` -- and because the
checks import it rather than the other way round.
"""

from dataclasses import dataclass, replace

from ..sources import SourceTable


ERROR = "error"


WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    """One thing the preflight noticed. `code` is stable and is what `allow=`
    names, so a project can carry a known-and-accepted finding in its
    deployment script without also silencing the ones it hasn't seen yet."""

    code: str
    level: str
    message: str

    def __str__(self) -> str:
        return f"{self.level.upper():<7} {self.code}  {self.message}"


@dataclass(frozen=True)
class SwapReport:
    model_label: str
    current: SourceTable | None
    candidate: SourceTable
    findings: tuple[Finding, ...]

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level == ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level == WARNING)

    @property
    def ok(self) -> bool:
        return not self.errors

    def __str__(self) -> str:
        header = (
            f"{self.model_label}: "
            f"{_qualified(self.current) if self.current else '(nothing deployed)'} "
            f"-> {_qualified(self.candidate)}"
        )
        if not self.findings:
            return f"{header}\n  no findings."
        body = "\n".join(f"  {finding}" for finding in self.findings)
        verdict = "would proceed" if self.ok else f"blocked by {len(self.errors)} error(s)"
        return f"{header}\n\n{body}\n\n  {verdict}."


def _qualified(source: SourceTable) -> str:
    return f"{source.schema}.{source.table}"


def _same_relation(left: SourceTable, right: SourceTable) -> bool:
    return (left.schema, left.table) == (right.schema, right.table)


def _allow(report: SwapReport, allowed) -> SwapReport:
    """The same report with the named codes downgraded to warnings. Downgraded
    rather than dropped, so an accepted finding still appears in the output --
    a swap that silently hid what it was told to ignore would be a worse tool
    than one that refused."""
    allowed = set(allowed)
    if not allowed:
        return report
    return replace(
        report,
        findings=tuple(
            replace(finding, level=WARNING, message=f"{finding.message} [allowed]")
            if finding.level == ERROR and finding.code in allowed
            else finding
            for finding in report.findings
        ),
    )
