"""Which checks make up the preflight, in what order, and what gates what.

verify_source_swap() is the whole read-only half of a swap: it resolves the
two sources, builds the one probe every check shares, runs them and hands back
a report. It changes nothing, which is what makes it safe to run against a
live source as often as you like -- and running it there, while the current
source is still serving, is the point of a blue-green source, because that is
the only moment when both tables exist to be compared.

The two tuples live here rather than beside the checks they name, because
which checks are in them is not a fact about any one check: the shape half
gates the row half, and the row half is the half the cutover re-runs under the
lock. Both of those are statements about the sequence, and this is the module
that owns the sequence.
"""

from django.db import connections

from ..sources import SourceTable
from ..sync import resolve_schema
from .columns import _check_columns, _check_extra_where
from .identity import _check_identity, _check_orphaned_base_rows
from .indexes import _check_indexes, _check_partitions
from .integrity import _check_dangling_references, _check_uniqueness
from .probes import _Probe, _relation_exists
from .report import ERROR, WARNING, Finding, SwapReport, _qualified
from .size import _check_row_estimate


# Everything about the candidate's shape: what columns it has, how it is
# indexed and partitioned, roughly how much is in it, and whether the
# predicate the view splices in still resolves against it. All of it has to
# hold before a single row-level probe is worth running, because every one of
# those names the columns these checks are about.
SHAPE_CHECKS = (_check_columns, _check_indexes, _check_partitions, _check_row_estimate, _check_extra_where)


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


def _resolve_identity_columns(model, identity_columns) -> tuple[tuple[str, ...], list[Finding]]:
    """Field names in, database columns out. Names are accepted because that is
    what the caller has in front of them in models.py; the probes need
    columns."""
    resolved, findings = [], []
    for name in identity_columns:
        try:
            resolved.append(model._meta.get_field(name).column)
        except Exception:  # noqa: BLE001 - FieldDoesNotExist, reported not raised
            findings.append(
                Finding("S016", ERROR, f"identity_columns names {name!r}, which {model.__name__} has no field for.")
            )
    return tuple(resolved), findings


def _run(probe: _Probe, checks) -> list[Finding]:
    findings = []
    for check in checks:
        findings.extend(check(probe))
    return findings


def verify_source_swap(
    model,
    candidate: SourceTable,
    *,
    current: SourceTable | None = None,
    identity_columns=(),
    using: str = "default",
    min_row_ratio: float = 0.9,
    checks=None,
) -> SwapReport:
    """Check `candidate` against everything the overlay currently guarantees,
    without changing anything.

    Run it while the current source is still live and serving -- that is the
    point of a blue-green source, and it is the only moment when both tables
    exist to be compared. `identity_columns` is the source's natural key, and
    leaving it out means the one check that matters most did not run; the
    report says so rather than passing quietly.

    `current` defaults to the configured source, which is right for a preflight
    run before config is flipped. swap_source() passes the *deployed* one
    instead, because by then config already names the candidate.
    """
    connection = connections[using]
    tenant_schema = resolve_schema(connection)
    current = current if current is not None else model.get_source()
    label = f"{model._meta.app_label}.{model.__name__}"

    identity, findings = _resolve_identity_columns(model, identity_columns)
    with connection.cursor() as cursor:
        for source in (current, candidate):
            if not _relation_exists(cursor, source):
                findings.append(Finding("S001", ERROR, f"{_qualified(source)} does not exist."))
        if any(f.code == "S001" for f in findings):
            return SwapReport(label, current, candidate, tuple(findings))

        probe = _Probe(
            cursor=cursor,
            model=model,
            tenant_schema=tenant_schema,
            current=current,
            candidate=candidate,
            identity_columns=identity,
            min_row_ratio=min_row_ratio,
            using=using,
        )
        if checks is not None:
            return SwapReport(label, current, candidate, tuple(findings + _run(probe, checks)))

        findings += _run(probe, SHAPE_CHECKS)
        if any(f.level == ERROR for f in findings):
            # Every row-level probe names the columns the shape checks just
            # found wrong. Running them anyway trades one precise finding for a
            # Postgres error from inside a query nobody asked to read.
            findings.append(
                Finding(
                    "S000",
                    WARNING,
                    "Skipped the row-level checks: the candidate's shape has to be right before "
                    "identity, references and uniqueness can be checked against it.",
                )
            )
            return SwapReport(label, current, candidate, tuple(findings))
        findings += _run(probe, ROW_CHECKS)
    return SwapReport(label, current, candidate, tuple(findings))
