"""How the candidate is indexed and partitioned, and what it costs to differ.

Nothing here blocks. Every finding is about cost -- a filtered read that turns
sequential the instant the view is replaced, a per-row trigger probe that fans
out across every partition -- and cost is not correctness, so a candidate that
trips only these is one that works and runs badly. They are warnings for the
same reason their `manage.py check` counterparts are: the source table is
somebody else's. They are still worth running, because the candidate is not
serving anything yet, and that makes a swap the one moment when somebody
else's table is yours to index properly.
"""

# Two privates reached across module lines on purpose. Both derive what a
# source table is required to look like from model state alone, which is
# exactly the question a preflight asks; re-deriving them here would be a
# second implementation of a rule that already has one, free to drift from the
# `manage.py check` that reports it the rest of the time.
from ..checks import _columns_needing_a_source_index, _index_columns
from ..introspection import compare_indexes, partition_summary, table_indexes
from .probes import _Probe
from .report import WARNING, Finding, _qualified


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
