"""Whether the candidate is the right *shape* to be swapped to.

What columns it has, how it is indexed and partitioned, roughly how much is
in it, and whether the predicate the view splices in still resolves against
it. All of it has to hold before a single row-level probe is worth running,
because every one of those names the columns these checks are about -- which
is why verify_source_swap() gates the one on the other.
"""

from django.db import transaction

from .._templating import render

# Two privates reached across module lines on purpose. Both derive what a
# source table is required to look like from model state alone, which is
# exactly the question a preflight asks; re-deriving them here would be a
# second implementation of a rule that already has one, free to drift from the
# `manage.py check` that reports it the rest of the time.
from ..checks import _columns_needing_a_source_index, _index_columns
from ..introspection import compare_indexes, partition_summary, table_indexes
from .probes import _column_types, _estimated_rows, _Probe, _required_columns
from .report import ERROR, WARNING, Finding, _qualified


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


def _check_indexes(probe: _Probe) -> list[Finding]:
    """The candidate carries the indexes the current source carries, and the
    ones this model's own triggers need.

    Both matter at different moments. Losing an index the current source has
    makes half of every filtered query a sequential scan the instant the view
    is replaced -- the W001 failure, arriving all at once. Missing one the
    triggers need makes every *write* scan the candidate, because a uniqueness
    trigger's probe and a foreign key's probe run per row.

    Warnings, like their `manage.py check` counterparts, and for the same
    reason: the source table is somebody else's. But a swap is the one moment
    when the indexes are yours to get right, because the candidate is not
    serving anything yet -- build them before you flip, not after."""
    findings = []
    current = table_indexes(probe.cursor, probe.current.schema, probe.current.table)
    candidate = table_indexes(probe.cursor, probe.candidate.schema, probe.candidate.table)
    lost, _ = compare_indexes(current, candidate)
    if lost:
        shapes = "\n".join(f"      - {index['shape']}" for index in lost)
        findings.append(
            Finding(
                "S010",
                WARNING,
                f"{_qualified(probe.candidate)} is missing {len(lost)} index(es) that "
                f"{_qualified(probe.current)} has:\n{shapes}",
            )
        )

    leading = {columns[0] for columns in map(_index_columns, (i["shape"] for i in candidate)) if columns}
    uncovered = {c: why for c, why in _columns_needing_a_source_index(probe.model).items() if c not in leading}
    if uncovered:
        lines = "\n".join(f"      - {column}: {why}" for column, why in sorted(uncovered.items()))
        findings.append(
            Finding(
                "S011",
                WARNING,
                f"{_qualified(probe.candidate)} has no index leading with:\n{lines}",
            )
        )
    return findings


def _check_partitions(probe: _Probe) -> list[Finding]:
    """The partition_key declaration still describes the table it names.

    A declaration is only ever about cost, so nothing here blocks -- but the
    two ways it can go wrong across a swap both cost the same thing, an
    unpruned Append over every partition on every probe, and neither shows up
    as anything but slowness."""
    findings = []
    summary = partition_summary(probe.cursor, probe.candidate.schema, probe.candidate.table)
    declared = probe.candidate.partition_key
    if declared and summary is None:
        findings.append(
            Finding(
                "S012",
                WARNING,
                f"partition_key={declared!r} is declared, but {_qualified(probe.candidate)} is not "
                "a partitioned table. Every generated probe carries a predicate that prunes nothing.",
            )
        )
    elif summary is not None and not declared:
        findings.append(
            Finding(
                "S012",
                WARNING,
                f"{_qualified(probe.candidate)} is partitioned into {summary['partitions']} partitions "
                "and the source declares no partition_key, so every generated probe fans out across "
                "all of them.",
            )
        )
    if summary is not None and summary["unattached"]:
        shapes = "\n".join(
            f"      - {entry['shape']} (on {entry['on_partitions']} partitions)" for entry in summary["unattached"]
        )
        findings.append(
            Finding(
                "S012",
                WARNING,
                f"{_qualified(probe.candidate)} has indexes built on individual partitions rather than "
                f"on the parent, so they cover some partitions and not others:\n{shapes}",
            )
        )
    return findings


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


# Everything about the candidate's shape: what columns it has, how it is
# indexed and partitioned, roughly how much is in it, and whether the
# predicate the view splices in still resolves against it. All of it has to
# hold before a single row-level probe is worth running, because every one of
# those names the columns these checks are about.
SHAPE_CHECKS = (_check_columns, _check_indexes, _check_partitions, _check_row_estimate, _check_extra_where)
