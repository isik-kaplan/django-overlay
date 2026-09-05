"""Whether the view's own query still resolves against the candidate.

Between them these two cover everything the view splices in by name: the
select list, which is the model's columns, and extra_where, which is the
source's own predicate. Both block, because both fail the same way if nobody
asks -- not at the cutover, which would at least be loud, but on the first
read after it, out of a view the operator has no reason to suspect.
"""

from django.db import transaction

from .._templating import render
from .probes import _column_types, _Probe, _required_columns
from .report import ERROR, Finding, _qualified


def _check_columns(probe: _Probe) -> list[Finding]:
    """Every column the view selects is present, and reads back as the same
    type it does today.

    Type equality is strict, modifier included, and it is worth being strict:
    varchar(100) widened to varchar(200) is a table the view will happily
    select from and a CharField(max_length=100) that now truncates on write.
    A deliberate widening is a model change with a migration behind it, not
    something to discover at cutover."""
    findings = []
    theirs = _column_types(probe.cursor, probe.candidate)
    ours = _column_types(probe.cursor, probe.current)
    for column in _required_columns(probe.model, probe.candidate):
        if column not in theirs:
            findings.append(
                Finding(
                    "S002",
                    ERROR,
                    f"{_qualified(probe.candidate)} has no column {column!r}, which the view selects.",
                )
            )
        elif column in ours and ours[column] != theirs[column]:
            findings.append(
                Finding(
                    "S002",
                    ERROR,
                    f"{column!r} is {ours[column]} on {_qualified(probe.current)} and "
                    f"{theirs[column]} on {_qualified(probe.candidate)}.",
                )
            )
    return findings


def _check_extra_where(probe: _Probe) -> list[Finding]:
    if not probe.candidate.extra_where:
        return []
    try:
        # A savepoint, because this probe is the one check that expects to
        # fail. A statement Postgres rejects aborts the transaction it is in,
        # and every remaining check shares that transaction -- without this,
        # finding the one thing extra_where can do wrong costs you the report
        # it was supposed to appear in.
        with transaction.atomic(using=probe.using):
            probe.cursor.execute(render("swaps/extra_where_probe.sql.j2", source=probe.candidate))
    except Exception as exc:  # noqa: BLE001 - whatever Postgres says is the finding
        return [
            Finding(
                "S014",
                ERROR,
                f"extra_where does not resolve against {_qualified(probe.candidate)}: "
                f"{str(exc).strip().splitlines()[0]}",
            )
        ]
    return []
