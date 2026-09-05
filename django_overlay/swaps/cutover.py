"""Reading what is deployed, running the preflight, and cutting over.

The three things a caller actually calls. Everything underneath is a check or
a report; this is the part that takes the lock and replaces the view.

SHAPE_CHECKS and ROW_CHECKS live here rather than beside the checks they name,
because which checks are in them is a statement about the sequence -- the
shape half gates the row half, and the row half is what the cutover re-runs
under the lock -- and this is the module that owns the sequence.
"""

from dataclasses import replace

from django.db import connections, transaction

from .._templating import render
from ..exceptions import OverlaySwapRefused
from ..sources import SourceTable
from ..sync import resolve_schema, statement_executor, sync_source_triggers, sync_view
from .columns import _check_columns, _check_extra_where
from .identity import _check_identity, _check_orphaned_base_rows
from .indexes import _check_indexes, _check_partitions
from .integrity import _check_dangling_references, _check_uniqueness
from .probes import _Probe, _relation_exists
from .report import ERROR, WARNING, Finding, SwapReport, _allow, _qualified, _same_relation
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


def deployed_source(connection, tenant_schema: str, model) -> SourceTable | None:
    """The source table the view is *actually* reading, read out of the
    catalogue rather than out of get_source().

    Those two answers differ for the whole length of a swap, which is the
    reason this exists: config is edited, the database is not, and the gap
    between them is exactly what the cutover closes. Asking the database means
    a swap cannot be run twice by accident, cannot be run against a view that
    was never deployed, and reports what is true rather than what is intended.

    Only the schema and the table come back. How the source is *read* --
    id_column, extra_where, partition_key -- is not recoverable from a view
    definition without parsing it, so swap_source() carries those over from the
    configured source and says so. A swap that also changes one of them is a
    different operation, and `current=` is how you spell it.
    """
    base_table = model._base_model._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(render("swaps/view_source_relations.sql.j2"), [tenant_schema, model._meta.db_table])
        relations = [
            (schema, table)
            for schema, table in cursor.fetchall()
            if not (schema == tenant_schema and table == base_table)
        ]
    if len(relations) != 1:
        # Zero means the view isn't there, or is source-less (which the
        # metaclass refuses, but a half-migrated database can still show).
        # More than one means something hand-edited the view, and guessing
        # which relation is "the source" is not this function's job.
        return None
    schema, table = relations[0]
    return SourceTable(schema=schema, table=table)


def swap_source(
    model,
    *,
    using: str = "default",
    identity_columns=(),
    current: SourceTable | None = None,
    dry_run: bool = False,
    lock_timeout: str = "5s",
    allow=(),
    min_row_ratio: float = 0.9,
) -> SwapReport:
    """Cut `model` over to the source its get_source() now returns.

    Flip config first, then call this: the configured source is the candidate,
    and the deployed one -- read back out of the view -- is what it is checked
    against. That ordering is the one that leaves nothing to revert. Config and
    database agree the moment this returns, so the next unrelated
    `resync_overlay_views` rebuilds what was just deployed instead of silently
    putting the old source back.

    The cutover is one transaction and deliberately so. It takes an EXCLUSIVE
    lock on the base table, re-runs the row-level checks while holding it --
    a preflight that ran a minute ago has not seen the writes that landed since,
    and a deferred foreign-key trigger validates against the source at the
    moment it fires, not the moment it commits -- and then replaces the view,
    its INSTEAD OF triggers, every uniqueness trigger and every inbound foreign
    key trigger together. Postgres does DDL transactionally, so what other
    sessions see is the old arrangement or the new one, never a view reading
    one table while its constraints probe another.

    Nothing here drops or alters the old source. Keeping it is what makes the
    swap reversible, and it is also the only place the tenant's real edit set
    can still be computed -- materialisation copies whole rows, so a base row
    diffed against the source row it was copied from is the only record of
    which columns a tenant actually touched. Once the old table is gone, so is
    that.
    """
    connection = connections[using]
    tenant_schema = resolve_schema(connection)
    candidate = model.get_source()
    label = f"{model._meta.app_label}.{model.__name__}"

    if current is None:
        current = deployed_source(connection, tenant_schema, model)
        if current is None:
            raise OverlaySwapRefused(
                SwapReport(
                    label,
                    None,
                    candidate,
                    (
                        Finding(
                            "S017",
                            ERROR,
                            f"Could not read a single source relation out of {model._meta.db_table}. "
                            "Either the view is not deployed (run migrations), or it reads something "
                            "this package did not write. Pass current= to say what to check against.",
                        ),
                    ),
                )
            )
        # How the source is read is carried over, not introspected -- see
        # deployed_source(). A swap that also changes id_column, extra_where or
        # partition_key has to say so with current=.
        current = replace(candidate, schema=current.schema, table=current.table)

    if _same_relation(current, candidate):
        return SwapReport(
            label,
            current,
            candidate,
            (
                Finding(
                    "S018",
                    WARNING,
                    f"{model._meta.db_table} already reads {_qualified(candidate)}. Nothing to swap — "
                    "if you meant to change it, edit get_source() first.",
                ),
            ),
        )

    verify = dict(
        current=current,
        identity_columns=identity_columns,
        using=using,
        min_row_ratio=min_row_ratio,
    )
    report = _allow(verify_source_swap(model, candidate, **verify), allow)
    if dry_run:
        return report
    if not report.ok:
        raise OverlaySwapRefused(report)

    with transaction.atomic(using=using), connection.cursor() as cursor:
        # set_config() rather than SET LOCAL, which takes no bind parameters.
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", [lock_timeout])
        cursor.execute(
            render(
                "swaps/lock_base_table.sql.j2",
                tenant_schema=tenant_schema,
                base_table=model._base_model._meta.db_table,
            )
        )
        # Only the row-level half is re-run. The shape half describes schema,
        # which nothing changes while this lock is held; the row half describes
        # rows, which is precisely what the lock was taken to freeze.
        recheck = _allow(verify_source_swap(model, candidate, checks=ROW_CHECKS, **verify), allow)
        if not recheck.ok:
            raise OverlaySwapRefused(recheck)
        execute = statement_executor(cursor)
        sync_view(model, tenant_schema, execute)
        sync_source_triggers(model, tenant_schema, execute)
    return report
