"""Blue-green swaps of a model's source table.

The overlay's integrity machinery is built entirely out of checks on *your*
writes. A uniqueness constraint's source-side half runs when you insert into
the base table; a foreign key's insert side runs when you write the reference;
its delete side runs when the target's base row is removed. Every one of them
is a row trigger on a table this package owns.

The source table is not one of those. Replacing it is millions of rows
appearing and disappearing without a single event any of those triggers can
see, so a swap can invalidate every invariant they exist to hold and none of
them will say a word. Two of the ways it does that are silent and permanent:

- **Renumbering.** The source's id is the overlay's identity. It is the view's
  primary key (negated, under NEGATIVE_ID), the thing a materialised row
  shadows, the thing a tombstone masks, and the value in every
  OverlayForeignKey column pointing at the model. A candidate that hands the
  same ids to different entities leaves all of that resolving perfectly, to the
  wrong rows.
- **Late collisions.** A value the source did not hold when you wrote yours,
  and holds now, is a uniqueness violation that no index and no trigger will
  ever raise.

So the swap is a procedure rather than a configuration change, and this package
is that procedure: verify_source_swap() runs each trigger's predicate backwards
as a set operation over the rows that already exist, and swap_source() takes
the lock, re-runs the blocking half of it, and cuts over -- view, INSTEAD OF
triggers, uniqueness triggers and inbound foreign-key triggers -- inside one
transaction, so nothing ever observes the view reading one table while the
constraints guarding it probe another.

What this will not do is decide for you. Everything it finds is reported; only
the findings that mean *silent* breakage block, and `allow=` downgrades any of
them for the case where you know better.

One module until it was split, and the split is by subject rather than by size:

    report  <-  probes  <-  shape
                       <-  rows   <-  cutover

Nothing points backwards, so the move was a move and not a rewrite. Everything
is re-exported here, so `from django_overlay.swaps import ...` means what it
did when this was one file -- including the private names the test suite reaches
for. The one thing a split does change is monkeypatching: a name has to be
patched on the module that *defines* it (`swaps.cutover.verify_source_swap`),
not on this one, where it is only a reference.
"""

# ruff: noqa: F401 -- every import here is a re-export, which is the point.
from .cutover import (
    _resolve_identity_columns,
    _run,
    deployed_source,
    swap_source,
    verify_source_swap,
)
from .probes import (
    _column_types,
    _estimated_rows,
    _Probe,
    _relation_exists,
    _required_columns,
)
from .report import (
    ERROR,
    WARNING,
    Finding,
    SwapReport,
    _allow,
    _qualified,
    _same_relation,
)
from .rows import (
    ROW_CHECKS,
    _check_dangling_references,
    _check_identity,
    _check_orphaned_base_rows,
    _check_uniqueness,
)
from .shape import (
    SHAPE_CHECKS,
    _check_columns,
    _check_extra_where,
    _check_indexes,
    _check_partitions,
    _check_row_estimate,
)


__all__ = [
    "ERROR",
    "WARNING",
    "Finding",
    "SwapReport",
    "deployed_source",
    "swap_source",
    "verify_source_swap",
]
