"""How long a run will take, predicted before it starts.

The prediction exists for one reason: so a `--scale 3.0` typed at four in the
afternoon says "this is about forty minutes" before it starts, rather than
after. It is also what the budget guard uses to decide whether to start a suite
at all.

Runtime does not scale linearly with data, so this does not pretend it does.
The ban suite is 25 seconds at 300,000 people and 128 at 1,000,000 -- five
times the work for three times the rows, because the shapes that run past the
cap at the larger size are exactly the ones the suite exists to measure. So each
suite carries measured points and this interpolates between them, extrapolating
from the last two beyond the top.

Every point is **seconds for one pass at a ten-second cap**, because that is
what CI runs and therefore the configuration worth being accurate about. The
cap matters because a cell that runs past it costs exactly the cap; raising the
cap makes the pathological cells, and only those, proportionally more
expensive. `_cap_factor` is a crude model of that -- crude in the safe
direction, since it over-predicts at wider caps.

Measured on 2026-08-17: 14 cores, Postgres 17.11 in docker, work_mem 4MB,
shared_buffers 128MB. A 2-vCPU CI runner is slower and the budget guard is what
handles that -- it skips what will not fit and says so, rather than overrunning.
"""

# scale -> seconds, at one pass and a 10s cap.
MEASURED = {
    # 0.05 and 1.0 measured directly. 3.0 extrapolated from the ban suite's
    # own 1.0 -> 3.0 growth, which is the only suite measured that high.
    # 0.05, 0.3 and 1.0 measured directly. 0.3 is there because it is what the
    # `standard` CI tier runs, and interpolating it from the neighbours was 24%
    # low -- linear between anchors under-predicts a curve that bends upward.
    # 3.0 is extrapolated from the ban suite's own 1.0 -> 3.0 growth, the only
    # one measured that high.
    "shapes": {0.05: 2, 0.3: 7, 1.0: 23, 3.0: 62},
    "hybrid": {0.05: 3, 0.3: 8, 1.0: 22, 3.0: 59},
    "selectivity": {0.05: 22, 0.3: 67, 1.0: 86, 3.0: 232},
    "aggregation": {0.05: 48, 0.3: 133, 1.0: 189, 3.0: 510},
    "staged": {0.05: 5, 0.3: 21, 1.0: 80, 3.0: 216},
    "set_algebra": {0.05: 2, 0.3: 11, 1.0: 45, 3.0: 122},
    # The best-measured suite: every point below is a real run (2.0 and 3.0
    # from a 60s-cap sweep, converted). Note how much steeper its curve is than
    # the others -- 32x from 0.05 to 1.0 where aggregation is 4x -- which is
    # the whole reason these are per-suite rather than one scaling rule.
    "ban": {0.05: 4, 0.3: 51, 1.0: 128, 2.0: 289, 3.0: 347},
    "hops": {0.05: 28, 0.3: 47, 1.0: 103, 3.0: 278},
}

# Cold graph build, in seconds. Independent of the cap -- it runs no queries
# that could hit one. Close to linear in rows, unlike everything above, because
# it is bulk inserts rather than planner behaviour.
#
# 1.0 measured at 139s. Restoring the same graph from the bench_cache schema
# instead is 63s, which is what the persistent docker volume buys.
BUILD = {0.02: 3, 0.05: 7, 0.3: 40, 1.0: 139, 3.0: 420}


def _cap_factor(cap_ms):
    """Points are taken at a 10s cap; wider caps cost more on capped cells."""
    return 0.5 + 0.5 * (cap_ms / 10_000)


def _interpolate(points, scale):
    known = sorted(points)
    if scale <= known[0]:
        # Below the smallest measured point, fall back to proportional. The
        # small end is dominated by fixed costs, so this over-estimates, which
        # is the direction to be wrong in.
        return points[known[0]] * max(scale / known[0], 0.1)
    if scale >= known[-1]:
        high, low = known[-1], known[-2]
        slope = (points[high] - points[low]) / (high - low)
        return points[high] + slope * (scale - high)
    for low, high in zip(known, known[1:], strict=False):
        if low <= scale <= high:
            span = (scale - low) / (high - low)
            return points[low] + span * (points[high] - points[low])
    raise AssertionError("unreachable")  # pragma: no cover


def for_suite(name, scale, passes=1, cap_ms=10_000):
    points = MEASURED.get(name)
    if not points:
        return None
    return _interpolate(points, scale) * passes * _cap_factor(cap_ms)


def for_build(scale):
    return _interpolate(BUILD, scale)


def for_run(names, scale, passes=1, cap_ms=10_000, cold_build=False):
    """(total seconds, {suite name: seconds}, build seconds)."""
    per_suite = {}
    for name in names:
        estimate = for_suite(name, scale, passes, cap_ms)
        if estimate is not None:
            per_suite[name] = estimate
    build = for_build(scale) if cold_build else 0.0
    return sum(per_suite.values()) + build, per_suite, build
