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

import time
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


def measure(build, cap_ms, rounds=3, give_up_after_ms=5_000):
    """(Cell, value) -- best of `rounds` after a discarded warm-up.

    A statement that trips the cap returns a capped Cell and a value of None,
    so callers can tell "slow" from "did not finish" and skip the equality
    check they cannot make against a missing answer.
    """
    try:
        build()
    except OperationalError:
        return Cell(float(cap_ms), capped=True), None

    best, value = None, None
    for _ in range(rounds):
        started = time.perf_counter()
        try:
            value = build()
        except OperationalError:
            return Cell(float(cap_ms), capped=True), None
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
