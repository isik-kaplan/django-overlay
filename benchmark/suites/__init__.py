"""The curated suites, and the context they run in.

A suite is a module exposing:

    NAME    a short identifier, what --suite takes
    TITLE   one line, printed as the heading
    run(ctx) -> iterable of harness.Section

Suites yield sections rather than returning a list, so each table prints the
moment it is finished instead of at the end of a run that may take half an
hour.
"""

import importlib
import time
from dataclasses import dataclass, field

from benchmark import harness


# Order matters: this is the order a full run executes in, and it is cheapest
# first. A run that hits its budget ceiling then loses the most expensive
# suites rather than an arbitrary selection of them.
SUITE_NAMES = (
    "shapes",
    "hybrid",
    "selectivity",
    "aggregation",
    "staged",
    "set_algebra",
    "ban",
    "hops",
    "fence",
)

# What a smoke run does: the two suites that would catch a genuine breakage,
# small enough to sit on every push.
SMOKE = ("shapes", "ban")

# A single execution gets this many times the statement cap before it is
# abandoned, with a floor for very small caps. The cap bounds Postgres; this
# bounds everything around it.
MEASUREMENT_CEILING_FACTOR = 6
MINIMUM_MEASUREMENT_CEILING = 30.0


def load_suite(name):
    return importlib.import_module(f"benchmark.suites.{name}")


def all_suites(names=None):
    return [load_suite(name) for name in (names or SUITE_NAMES)]


@dataclass
class Context:
    """What a suite is handed: the knobs, and somewhere to report to."""

    scale: float
    passes: int
    cap_ms: int
    say: object = print
    # Absolute time the whole run must be finished by, or None for no ceiling.
    deadline: float = None
    # Rows where the overlay and the plain mirror returned different answers.
    # This is the only thing a benchmark run can genuinely fail on -- a timing
    # is never a failure, but a wrong answer is.
    disagreements: list = field(default_factory=list)

    def out_of_time(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    def measure(self, build, rounds=3):
        """Time `build`, unless the run has already used up its budget.

        Every suite measures through here, which makes it the one place a
        wall-clock ceiling can be enforced *inside* a suite. The runner's own
        check only happens between suites, and that is not enough: the
        statement cap bounds how long Postgres will spend on a query, but not
        how long Django spends marshalling a third of a million primary keys
        into one, and the staged suite does exactly that at scale 1.0.

        Checking before each measurement is necessary but not sufficient: it
        cannot interrupt the measurement it is already inside. So every
        execution also carries a ceiling of its own -- see
        harness.abandon_after -- and a measurement that runs past it says
        "gave up" rather than running until the CI job is killed.

        A skipped cell says "skipped", never a duration -- see harness.Cell.
        """
        if self.out_of_time():
            return harness.Cell(0.0, note="skipped"), None
        return harness.measure(build, self.cap_ms, rounds=rounds, abandon_after_s=self.measurement_ceiling())

    def measurement_ceiling(self):
        """How long one execution may take before it is abandoned.

        Generous against the statement cap, because a single execution may be
        several statements and the client-side work between them is real, but
        finite. Never longer than what is left of the whole run's budget --
        there is no point letting one row eat time the remaining suites need.
        """
        ceiling = max(MINIMUM_MEASUREMENT_CEILING, self.cap_ms / 1000 * MEASUREMENT_CEILING_FACTOR)
        if self.deadline is not None:
            ceiling = min(ceiling, max(1.0, self.deadline - time.monotonic()))
        return ceiling

    def compare(self, label, expected, actual, what="overlay and plain disagree"):
        """Record a disagreement rather than raising.

        A benchmark that aborts on the first wrong answer tells you about one
        problem and hides the rest, and it throws away every timing measured
        after it. Collect them; the CLI exits non-zero at the end if the list
        is not empty.
        """
        if expected is None or actual is None:
            return True
        if expected != actual:
            self.disagreements.append(f"{label}: {what} ({expected!r} vs {actual!r})")
            return False
        return True
