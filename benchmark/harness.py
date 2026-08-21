"""Timing, tables, and the runtime budget.

Three things every suite in this directory needs and none of them should
reimplement:

  * `measure()` -- a warm-up round discarded, then best-of-N, with a statement
    timeout that turns "this never finishes" into a value rather than a hang.
    The warm-up is not politeness. An earlier version of the ban probe took
    best-of-two with no warm-up and produced a table that contradicted its own
    previous run: a query with no joins at all, unbanned in both columns, moved
    from 13ms to 231ms between runs. Nothing under test can do that.

  * `Section` -- a table of rows, rendered to ascii for a terminal, markdown
    for a CI step summary, and plain data for the results file. Sections print
    as soon as they finish rather than at the end, so a forty-minute run shows
    its work as it goes. That streaming *is* the progress reporting; there is
    no separate chatter, because a progress line the reader learns to skip is
    worse than no line.

  * `Budget` -- a wall-clock ceiling. When it runs out the remaining suites are
    skipped and say so in the output. An estimate that is wrong only misleads;
    a budget that is enforced means a bad run costs an hour rather than an
    afternoon.
"""

import signal
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from django.db import OperationalError, connection


@dataclass
class Cell:
    """One measured number, or a reason there isn't one.

    Three states, and they mean different things: a duration, a cell that ran
    past the cap (a lower bound, not a number), and a cell that was never run
    at all. Collapsing the last two would be the worst kind of wrong -- "we
    skipped this" reported as "this is slow".
    """

    ms: float
    capped: bool = False
    note: str = ""

    def render(self, cap_ms):
        if self.note:
            return self.note
        if self.capped:
            return f">{cap_ms // 1000}s"
        return f"{self.ms:.0f}ms"

    @property
    def measured(self):
        return not self.capped and not self.note


@dataclass
class Row:
    label: str
    cells: dict = field(default_factory=dict)
    extras: dict = field(default_factory=dict)


@dataclass
class Section:
    """A titled table.

    `columns` is the full ordered list of headings after the label. A column
    holding a Cell renders as a duration; one holding anything else renders as
    text. That is what lets "gain" and "rows" sit between and after the timing
    columns instead of being herded to the end.
    """

    title: str
    columns: tuple
    note: str = ""
    rows: list = field(default_factory=list)

    def add(self, label, cells=None, **extras):
        self.rows.append(Row(label, cells or {}, extras))
        return self.rows[-1]


def set_statement_cap(milliseconds):
    with connection.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = {milliseconds}")
        cursor.execute("SET lock_timeout = 5000")


class Abandoned(Exception):
    """Raised inside a measurement that has run past its wall-clock ceiling."""


@contextmanager
def abandon_after(seconds):
    """Raise `Abandoned` inside the block if it runs longer than `seconds`.

    The statement cap bounds what Postgres will spend on a query. Nothing
    bounded what Django spends around it, and the two are not close: a staged
    strategy resolves one leaf, pulls its primary keys into Python, and hands
    six figures of them back as a `pk__in`, which is compiled, parameterised
    and marshalled entirely client-side. Observed in CI -- `staged` at scale
    0.3, estimated at twenty-one seconds, was thirty-seven minutes into a
    single measurement when the job timeout killed the run. The budget could
    not stop it because a budget checked between measurements cannot interrupt
    the measurement it is inside.

    A signal is the only thing that reaches into a call already in progress.
    It requires the main thread of a POSIX process; anywhere else this is a
    no-op, so the ceiling degrades to "not enforced" rather than to a crash.
    """
    usable = (
        seconds is not None
        and seconds > 0
        and hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )
    if not usable:
        yield
        return

    def fire(signum, frame):
        raise Abandoned

    previous = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _connection_state():
    """What libpq thinks the connection is doing, for the log.

    0 is idle, 1 is a command still in progress, 3 is a failed transaction.
    Reported rather than acted on: this exists because the wedge it is meant to
    explain has never reproduced outside CI -- see TODO/21 -- and a guess about
    which state the connection was left in is worth less than the state.
    """
    raw = getattr(connection, "connection", None)
    if raw is None:
        return "none"
    try:
        return str(raw.pgconn.transaction_status)
    except Exception:  # noqa: BLE001 -- diagnostics must not raise
        return "unreadable"


def _abandoned():
    """The cell for a measurement that was cut off, plus the cleanup it needs.

    The interrupt can land anywhere -- including between sending a statement
    and reading its result -- so the connection is not in a state worth
    reasoning about. Closing it is the one reset that is certain, and Django
    reopens on next use.
    """
    before = _connection_state()
    connection.close()
    print(f"ABANDONED a measurement; connection was {before}, "
          f"now {_connection_state()}", file=sys.stderr)
    return Cell(0.0, note="gave up")


# The SQLSTATEs that mean "a cap did its job". Everything else arriving as an
# OperationalError means the connection itself is in trouble, and the two must
# not share a cell: see _cap_or_lost.
CAP_SQLSTATES = frozenset({
    "57014",  # query_canceled -- statement_timeout
    "55P03",  # lock_not_available -- lock_timeout
})


def _sqlstate(error):
    """The Postgres error code behind a Django OperationalError, if there is one.

    Django re-raises the driver's exception as its own with the original
    attached, so the code lives on __cause__ rather than on what we catch.
    """
    return getattr(error.__cause__, "sqlstate", None)


# The note a lost-connection cell carries. Named because runner.py counts them
# and results.py refuses to offer such a run as a baseline, and a string literal
# repeated in three files is one rename away from a guard that matches nothing.
LOST_NOTE = "conn lost"


def lost_cells(suites):
    """How many cells in a saved-shape run were never measured at all.

    Separate from the run so it can be tested without a database. A guard that
    counts by the wrong key is a guard that never fires, which is worse than not
    having one -- it reads as "nothing went wrong".
    """
    return sum(
        1
        for suite in suites
        for section in suite.get("sections", ())
        for row in section.get("rows", ())
        for cell in row.get("cells", {}).values()
        if cell.get("note") == LOST_NOTE
    )


def _cap_or_lost(error, cap_ms):
    """Tell "this query is too slow" apart from "this connection is broken".

    Both surface as OperationalError, and treating the second as the first is
    how a benchmark reports numbers it never measured. Observed in CI: a
    connection was left with an unconsumed result part-way through `staged`,
    every statement after it failed instantly with "another command is already
    in progress", and all five rows of the next section printed `>10s did not
    finish` -- five queries that were never sent, filed as five queries too
    slow to finish. The suite after that died on its first statement, which is
    the only reason it was noticed at all.

    A cap is a real answer about the query, so it stays a capped cell. A broken
    connection is not an answer at all: it gets a note, closing the connection
    so the next measurement starts from something usable, and one line on
    stderr, because the reason a connection broke is not in the table.
    """
    if _sqlstate(error) in CAP_SQLSTATES:
        return Cell(float(cap_ms), capped=True)
    before = _connection_state()
    connection.close()
    first_line = str(error).strip().splitlines()[0] if str(error).strip() else error.__class__.__name__
    print(f"LOST CONNECTION [was {before}] {first_line}", file=sys.stderr)
    return Cell(0.0, note=LOST_NOTE)


def measure(build, cap_ms, rounds=3, give_up_after_ms=5_000, abandon_after_s=None):
    """(Cell, value) -- best of `rounds` after a discarded warm-up.

    A statement that trips the cap returns a capped Cell and a value of None,
    so callers can tell "slow" from "did not finish" and skip the equality
    check they cannot make against a missing answer. A measurement that runs
    past `abandon_after_s` returns a "gave up" cell, which is a different
    thing again: the cap was not reached, the wall clock was. A connection that
    breaks gets a third answer -- "conn lost" -- rather than borrowing the
    cap's.
    """
    try:
        with abandon_after(abandon_after_s):
            build()
    except OperationalError as error:
        return _cap_or_lost(error, cap_ms), None
    except Abandoned:
        return _abandoned(), None

    best, value = None, None
    for _ in range(rounds):
        started = time.perf_counter()
        try:
            with abandon_after(abandon_after_s):
                value = build()
        except OperationalError as error:
            return _cap_or_lost(error, cap_ms), None
        except Abandoned:
            return _abandoned(), None
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
        # Something this slow will not become interesting on a third try, and
        # at the top of a scale sweep those rounds are minutes.
        if best > give_up_after_ms:
            break
    return Cell(best), value


def gain(off: Cell, on: Cell):
    """How much faster the `on` column is. Blank unless both were measured.

    A capped cell only says "at least this slow", so a ratio built from one
    would understate the gain and read as a precise number while doing it. A
    skipped cell has no number at all.
    """
    if not off.measured or not on.measured or not on.ms:
        return ""
    return f"x{off.ms / on.ms:.1f}"


# ------------------------------------------------------------------ budget


class BudgetExhausted(Exception):
    pass


@dataclass
class Budget:
    """A wall-clock ceiling for the whole run."""

    max_seconds: float
    # monotonic rather than perf_counter so the deadline handed to a suite's
    # Context is on the same clock the suite compares against.
    started: float = field(default_factory=time.monotonic)

    def spent(self):
        return time.monotonic() - self.started

    def remaining(self):
        return max(0.0, self.max_seconds - self.spent())

    def deadline(self):
        return self.started + self.max_seconds

    def can_afford(self, estimate_seconds):
        """Whether to start a suite at all.

        Starting something that is estimated to overrun is worse than skipping
        it: the run blows the ceiling *and* the partial table it produces is
        not comparable with anything.
        """
        return self.remaining() >= estimate_seconds


def humanise(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


# ------------------------------------------------------------- rendering


def _widths(section, cap_ms, deltas):
    label = max([len("query")] + [len(row.label) for row in section.rows])
    columns = {}
    for name in section.columns:
        rendered = [_cell_text(row, name, cap_ms, deltas) for row in section.rows]
        columns[name] = max([len(name)] + [len(text) for text in rendered])
    return label, columns


def _cell_text(row, name, cap_ms, deltas):
    cell = row.cells.get(name)
    if cell is None:
        return str(row.extras.get(name, ""))
    text = cell.render(cap_ms)
    delta = (deltas or {}).get((row.label, name))
    if delta:
        text = f"{text} {delta}"
    return text


def render_ascii(section, cap_ms, deltas=None):
    label_width, columns = _widths(section, cap_ms, deltas)
    lines = ["", section.title, "-" * max(len(section.title), 40)]
    if section.note:
        lines += [f"  {section.note}", ""]

    head = f"  {'query':<{label_width}}"
    for name in section.columns:
        head += f" {name:>{columns[name]}}"
    lines.append(head)
    lines.append("  " + "-" * (len(head) - 2))

    for row in section.rows:
        line = f"  {row.label:<{label_width}}"
        for name in section.columns:
            line += f" {_cell_text(row, name, cap_ms, deltas):>{columns[name]}}"
        lines.append(line)
    return "\n".join(lines)


def render_markdown(section, cap_ms, deltas=None):
    headers = ["query", *section.columns]
    lines = [f"**{section.title}**", ""]
    if section.note:
        lines += [section.note, ""]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in section.rows:
        cells = [row.label]
        cells += [_cell_text(row, name, cap_ms, deltas) for name in section.columns]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def section_to_data(section):
    """The shape that goes into a saved results file."""
    return {
        "title": section.title,
        "columns": list(section.columns),
        "note": section.note,
        "rows": [
            {
                "label": row.label,
                "cells": {
                    name: {"ms": cell.ms, "capped": cell.capped, "note": cell.note}
                    for name, cell in row.cells.items()
                },
                "extras": {name: str(value) for name, value in row.extras.items()},
            }
            for row in section.rows
        ],
    }
