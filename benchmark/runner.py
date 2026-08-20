"""Executes a run: load the graph, walk the suites, enforce the budget.

The output discipline here is deliberate. A benchmark that narrates every row
teaches the reader to skip its output, and then the one line that mattered --
the budget, a disagreement, a skipped suite -- goes past unread. So:

  * the tables are the progress. Each prints the moment its section finishes.
  * the budget is stated loudly once at the start and once at the end, in a
    banner, because it is the thing that explains a short run.
  * everything else is one line per event, and there are only four kinds of
    event: the graph loaded, a suite started, a suite was skipped, a suite
    finished.
"""

import time

from benchmark import environment, estimates, graph, harness, results
from benchmark.suites import Context, all_suites


BANNER = "=" * 78


def banner(say, title, lines):
    say("")
    say(BANNER)
    say(title)
    for line in lines:
        say(f"  {line}")
    say(BANNER)


def run(
    suite_names,
    scale,
    passes,
    cap_ms,
    max_runtime,
    say=print,
    baseline=None,
    rebuild=False,
    share=None,
):
    """Execute the suites and return everything a caller needs to report.

    Returns a dict with the environment, the per-suite data, the disagreements,
    and the wall-clock spent -- the CLI turns that into console output, a saved
    results file, and an exit code.
    """
    graph.configure(scale=scale, share=share, timeout_ms=cap_ms)
    suites = all_suites(suite_names)
    names = [suite.NAME for suite in suites]

    cold = not graph.cache_is_warm() or rebuild
    predicted, per_suite, build_estimate = estimates.for_run(
        names, scale, passes, cap_ms, cold_build=cold
    )

    banner(say, "  BUDGET", [
        f"ceiling      {harness.humanise(max_runtime)}",
        f"estimated    {harness.humanise(predicted)}"
        + (f"  (including ~{harness.humanise(build_estimate)} to build the graph)" if cold else ""),
        f"suites       {', '.join(names)}",
        f"settings     scale {scale}, {passes} pass(es), {cap_ms // 1000}s cap",
    ])

    budget = harness.Budget(max_runtime)

    seconds, built = graph.load(rebuild=rebuild, progress=say)
    say(f"\ngraph {'built' if built else 'restored from cache'} in {harness.humanise(seconds)}"
        f" -- {graph.PERSON_VIEW:,} people")

    harness.set_statement_cap(cap_ms)
    env = environment.capture(scale=scale, share=graph.SHARE, cap_ms=cap_ms, passes=passes)
    say(environment.summarise(env))

    note = results.comparison_note(baseline, env)
    usable_baseline = baseline if (note and note.startswith("delta")) else None
    if note:
        say(note)

    ctx = Context(scale=scale, passes=passes, cap_ms=cap_ms, say=say,
                  deadline=budget.deadline())
    collected, skipped, truncated = [], [], []

    for suite in suites:
        estimate = per_suite.get(suite.NAME, 0)
        if not budget.can_afford(estimate):
            skipped.append(suite.NAME)
            say(f"\nSKIPPED {suite.NAME} -- needs about {harness.humanise(estimate)}, "
                f"{harness.humanise(budget.remaining())} left in the budget")
            collected.append({"name": suite.NAME, "title": suite.TITLE,
                              "status": "skipped-budget", "seconds": 0, "sections": []})
            continue

        say(f"\n\n{BANNER}\n  {suite.TITLE}\n  [{suite.NAME}] about {harness.humanise(estimate)}"
            f", {harness.humanise(budget.remaining())} of budget left\n{BANNER}")

        # Re-applied per suite rather than once at the start, because a suite
        # can change session state. graph.best_of() used to clear the timeout
        # on its way out, which left every suite after `shapes` running with no
        # cap at all -- found when a query in `staged` ran for three minutes
        # against a ten-second cap. That is fixed at the source now; this stays
        # as the cheap guarantee that no future suite can do it again silently.
        harness.set_statement_cap(cap_ms)

        started = time.perf_counter()
        sections = []
        for section in suite.run(ctx):
            deltas = results.deltas_for(usable_baseline, suite.NAME, section)
            say(harness.render_ascii(section, cap_ms, deltas))
            sections.append(harness.section_to_data(section))
        elapsed = time.perf_counter() - started

        # A suite that ran out of budget part-way through still yields its
        # sections, with the unmeasured rows marked "skipped". Saying so is the
        # whole point -- a table with holes in it that does not admit to them
        # reads as a complete result.
        cut_short = ctx.out_of_time()
        if cut_short:
            truncated.append(suite.NAME)
        collected.append({"name": suite.NAME, "title": suite.TITLE,
                          "status": "truncated-budget" if cut_short else "ok",
                          "seconds": elapsed, "sections": sections})
        say(f"\n  [{suite.NAME}] {'CUT SHORT' if cut_short else 'done'} "
            f"in {harness.humanise(elapsed)}")

    spent = budget.spent()
    lines = [
        f"ceiling      {harness.humanise(max_runtime)}",
        f"estimated    {harness.humanise(predicted)}",
        f"actual       {harness.humanise(spent)}",
    ]
    if skipped:
        lines.append(f"SKIPPED      {', '.join(skipped)} -- ran out of budget")
    if truncated:
        lines.append(f"SKIPPED      rows inside {', '.join(truncated)} -- ran out of budget")
    if ctx.disagreements:
        lines.append(f"DISAGREED    {len(ctx.disagreements)} row(s) -- see below")
    banner(say, "  BUDGET", lines)

    if ctx.disagreements:
        say("\nRows where the overlay and the plain mirror did not agree:")
        for entry in ctx.disagreements:
            say(f"  - {entry}")

    return {
        "environment": env,
        "suites": collected,
        "disagreements": ctx.disagreements,
        "skipped": skipped,
        "truncated": truncated,
        "seconds": spent,
        "estimated": predicted,
        "baseline": usable_baseline["label"] if usable_baseline else None,
    }
