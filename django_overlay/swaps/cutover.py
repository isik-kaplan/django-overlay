"""Taking the lock and replacing the view.

The one part of a swap that changes anything. Everything underneath it is a
check or a report; this is where the preflight's verdict is acted on, and it
is deliberately one transaction: the lock is taken, the row-level half of the
preflight is re-run while holding it, and the view, its INSTEAD OF triggers,
every uniqueness trigger and every inbound foreign-key trigger are replaced
together.

deployed_source() belongs here rather than with the other catalogue reads
because it is not a question about a source table at all -- it is the question
"what did we deploy last time", and swap_source() is the only thing that has a
reason to ask it.
"""

from dataclasses import replace

from django.db import connections, transaction

from .._templating import render
from ..exceptions import OverlaySwapRefused
from ..sources import SourceTable
from ..sync import resolve_schema, statement_executor, sync_source_triggers, sync_view
from .preflight import ROW_CHECKS, verify_source_swap
from .report import ERROR, WARNING, Finding, SwapReport, _allow, _qualified, _same_relation


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
