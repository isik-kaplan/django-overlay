"""`django-overlay benchmark` -- the command cloners run.

Not shipped in the wheel. The installed package carries a shim that imports
this lazily and explains itself when the directory is absent; see
`django_overlay/cli.py`.

Everything is a flag, and `--interactive` asks for the same things in order for
anyone who would rather not remember them.
"""

import json
import os
import re
import sys

import click


DEFAULT_MAX_RUNTIME = 3600


def parse_duration(value):
    """'1h', '45m', '600s', or a bare number of seconds."""
    if value is None:
        return None
    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([hms]?)", text)
    if not match:
        raise click.BadParameter(f"cannot read {value!r} as a duration -- try 1h, 45m or 600")
    amount, unit = float(match.group(1)), match.group(2)
    return amount * {"h": 3600, "m": 60, "s": 1, "": 1}[unit]


def _suite_names():
    from benchmark.suites import SUITE_NAMES
    return SUITE_NAMES


@click.command(context_settings={"max_content_width": 100})
@click.option("--scale", type=float, default=0.3, show_default=True,
              help="1.0 is 1,000,000 people. Runtime grows faster than linearly.")
@click.option("--share", type=float, default=None,
              help="Fraction of each view held by the tenant's base table (default 0.4).")
@click.option("--suite", "suites", multiple=True,
              help="Run only these suites. Repeatable. Default is all of them.")
@click.option("--smoke", is_flag=True, help="The two cheapest suites, for a quick check.")
@click.option("--list-suites", is_flag=True, help="Print the suite names and exit.")
@click.option("--passes", type=int, default=2, show_default=True,
              help="Repeat each suite. Two tells signal from drift; CI uses one.")
@click.option("--cap", "cap_seconds", type=int, default=30, show_default=True,
              help="Statement timeout. A query past it is reported as capped, not waited on.")
@click.option("--max-runtime", default="1h", show_default=True,
              help="Hard ceiling. Suites that will not fit are skipped and said so.")
@click.option("--database-url", default=None,
              help="Run against this Postgres instead of docker compose.")
@click.option("--postgres-version", default="17", show_default=True,
              help="Image tag for the compose database. 16 is what CI uses.")
@click.option("--work-mem", default="4MB", show_default=True,
              help="work_mem for the compose database. Decides whether a hash join spills.")
@click.option("--shared-buffers", default="128MB", show_default=True,
              help="shared_buffers for the compose database.")
@click.option("--port", type=int, default=55432, show_default=True,
              help="Host port for the compose database.")
@click.option("--rebuild", is_flag=True, help="Regenerate the graph even if the cache matches.")
@click.option("--keep-up/--down", default=True, show_default=True,
              help="Leave the compose database running afterwards.")
@click.option("--format", "output_format",
              type=click.Choice(["table", "markdown", "json"]), default="table",
              show_default=True, help="table for a terminal, markdown for a CI summary.")
@click.option("--output", type=click.Path(dir_okay=False, writable=True), default=None,
              help="Write the chosen format here as well as to the terminal.")
@click.option("--save-results", is_flag=True, help="Save this run as a baseline for later runs.")
@click.option("--label", default=None, help="Name for the saved run. Defaults to the git sha.")
@click.option("--compare-to", default=None,
              help="Compare against this saved label instead of the most recent.")
@click.option("--no-compare", is_flag=True, help="Do not add a delta column.")
@click.option("--clear-results", is_flag=True, help="Delete every saved run and exit.")
@click.option("--interactive", is_flag=True, help="Ask for the settings instead of taking flags.")
# The four library optimisations. Paired flags with no default of their own, so
# that "not given" is distinguishable from "given as on" -- which is what lets
# --no-optimisations move the floor under all four while an explicit
# --force-hash-joins lifts one back out of it. Each flag's name and help text
# is the same string as its entry in benchmark/switches.py, which is where
# settings.py and the environment record read them from; a test asserts the
# two spellings agree, because a flag the table does not know about moves
# nothing.
@click.option("--no-optimisations", "all_off", is_flag=True,
              help="Turn all four query optimisations off. The other arm of the A/B.")
@click.option("--rewrite-traversals/--no-rewrite-traversals", default=None,
              help="Rewrite a filter that traverses between two overlay views into a subquery.")
@click.option("--redirect-select-related/--no-redirect-select-related", default=None,
              help="Route select_related() across views through prefetch_related() instead.")
@click.option("--force-hash-joins/--no-force-hash-joins", default=None,
              help="Ban nested loops for a query joining several overlay views.")
@click.option("--array-subquery-in/--no-array-subquery-in", default=None,
              help="Fence an __in subquery as `lhs = ANY (ARRAY(subquery))`.")
@click.option("--yes", is_flag=True, help="Do not prompt before a long run.")
def benchmark(**options):
    """Measure django-overlay against plain tables holding identical rows."""
    from benchmark import estimates, results

    if options["clear_results"]:
        removed = results.clear()
        click.echo(f"removed {removed} saved run(s) from {results.DIRECTORY}")
        return

    if options["interactive"]:
        options = _ask(options)

    # Ahead of everything that brings Django up, because settings.py reads
    # these off the environment at import time and django.setup() imports it.
    # A flag resolved after that point would be reported as set and obeyed by
    # nothing -- which is what an A/B that measured one arm twice and called it
    # a result looks like from the outside.
    _apply_switches(options)

    if options["list_suites"]:
        # Importing a suite reaches django_overlay.models, which reads
        # settings at import time, so the app registry has to be up even for
        # something as passive as printing names. No database is touched.
        _configure_django()
        _print_suites()
        return

    suites = list(options["suites"])
    if options["smoke"]:
        from benchmark.suites import SMOKE
        suites = list(SMOKE)
    unknown = [name for name in suites if name not in _suite_names()]
    if unknown:
        raise click.BadParameter(f"unknown suite(s): {', '.join(unknown)}")
    names = suites or list(_suite_names())

    scale = options["scale"]
    passes = options["passes"]
    cap_ms = options["cap_seconds"] * 1000
    max_runtime = parse_duration(options["max_runtime"])

    # The warning comes before anything slow happens -- before docker, before
    # the schema, before the graph. Knowing at second one that this is a
    # forty-minute job is the whole point; knowing it at minute five is not.
    # The graph is assumed cold here because we cannot see the database yet,
    # which makes this the pessimistic figure. The runner prints the real one.
    predicted, _, build = estimates.for_run(names, scale, passes, cap_ms, cold_build=True)
    click.echo(f"about {_humanise(predicted)} at worst "
               f"(up to {_humanise(build)} of that building the graph, if it is not cached)")
    if predicted > max_runtime:
        click.echo(click.style(
            f"WARNING: that is past the {_humanise(max_runtime)} ceiling. Suites that do not "
            f"fit will be skipped. Raise it with --max-runtime, or lower --scale.",
            fg="yellow",
        ))
        if not options["yes"] and not click.confirm("Run anyway?", default=True):
            return

    database_url = options["database_url"]
    started_docker = False
    if not database_url:
        from benchmark import docker
        try:
            database_url = docker.up(
                postgres_version=options["postgres_version"],
                work_mem=options["work_mem"],
                shared_buffers=options["shared_buffers"],
                host_port=options["port"],
                say=click.echo,
            )
        except docker.DockerUnavailable as error:
            # A missing Docker is an ordinary situation with an obvious next
            # step, not a bug worth a traceback.
            raise click.ClickException(str(error)) from error
        started_docker = True

    _setup_django(database_url)

    from benchmark import runner

    baseline = None
    if not options["no_compare"]:
        baseline = results.latest(options["compare_to"])
        if options["compare_to"] and baseline is None:
            raise click.BadParameter(f"no saved run labelled {options['compare_to']!r}")

    outcome = runner.run(
        names, scale=scale, passes=passes, cap_ms=cap_ms, max_runtime=max_runtime,
        say=click.echo, baseline=baseline, rebuild=options["rebuild"], share=options["share"],
    )

    _write_output(outcome, options, cap_ms)

    if options["save_results"]:
        # The default label is the sha, which is the same string for both arms
        # of a switch A/B -- so the second run would overwrite the first and
        # the comparison would be against itself. The arm goes in the name.
        label = options["label"] or _default_label(outcome["environment"])
        path = results.save(label, outcome["environment"], outcome["suites"])
        click.echo(f"\nsaved as {path}")

    if started_docker and not options["keep_up"]:
        from benchmark import docker
        docker.down(postgres_version=options["postgres_version"], say=click.echo)

    if outcome["disagreements"]:
        raise SystemExit(1)


def _default_label(env):
    """The git sha, plus which optimisations were off if any were.

    Four names concatenated would be a filename nobody can read, and the
    all-off arm is the common one, so it gets a name of its own.
    """
    from benchmark import switches
    off = switches.describe(env.get("switches") or {})
    if not off:
        return env["git_sha"]
    if len(off) == len(switches.SWITCHES):
        return f"{env['git_sha']}-no-optimisations"
    return env["git_sha"] + "".join(f"-no-{flag}" for flag in off)


def _print_suites():
    from benchmark import estimates
    from benchmark.suites import SMOKE, all_suites
    click.echo(f"{'suite':<14} {'~1.0':>7}  title")
    for suite in all_suites():
        estimate = estimates.for_suite(suite.NAME, 1.0) or 0
        marker = " *" if suite.NAME in SMOKE else "  "
        click.echo(f"{suite.NAME:<14} {_humanise(estimate):>7}{marker}{suite.TITLE}")
    click.echo("\n* included in --smoke")


def _ask(options):
    """The same settings, prompted for in order."""
    options = dict(options)
    options["scale"] = click.prompt(
        "Scale (1.0 = 1,000,000 people)", type=float, default=options["scale"])
    options["passes"] = click.prompt(
        "Passes over each suite", type=int, default=options["passes"])
    options["cap_seconds"] = click.prompt(
        "Statement cap in seconds", type=int, default=options["cap_seconds"])
    options["max_runtime"] = click.prompt(
        "Ceiling for the whole run", default=options["max_runtime"])

    if click.confirm("Run every suite?", default=not options["suites"]):
        options["suites"] = ()
    else:
        chosen = click.prompt("Which suites (comma separated)",
                              default=",".join(_suite_names()))
        options["suites"] = tuple(name.strip() for name in chosen.split(",") if name.strip())

    from benchmark import docker
    if docker.available():
        if not click.confirm("Use the docker compose database?", default=True):
            options["database_url"] = click.prompt("Postgres URL")
    else:
        click.echo("docker is not available here.")
        options["database_url"] = click.prompt("Postgres URL", default=options["database_url"])

    # Asked as one question rather than four, because the four-off arm is what
    # anybody prompting their way through this actually wants, and offering
    # sixteen combinations to somebody who did not want to remember a flag name
    # is not a kindness. The individual flags stay available for the case the
    # combinations are the point.
    from benchmark import switches
    if not click.confirm("Leave every query optimisation on?", default=not options["all_off"]):
        options["all_off"] = True
        for switch in switches.SWITCHES:
            name = switches.option_name(switch)
            options[name] = click.confirm(f"  keep {switch.flag}?", default=False)

    options["save_results"] = click.confirm(
        "Save this run as a baseline?", default=options["save_results"])
    # `yes` is deliberately left alone. Somebody who just typed a scale into a
    # prompt is exactly the person the over-budget confirmation is for.
    return options


def _apply_switches(options):
    """Resolve the optimisation flags and put them where settings.py reads them.

    Announced when anything is off, and announced loudly. A run with the
    rewrites disabled produces numbers eighty times worse than the same command
    without the flag, and a table of those pasted into an issue with no header
    saying which arm it was is worse than no measurement at all.
    """
    from benchmark import switches
    chosen = switches.resolve(options, all_off=options["all_off"])
    switches.apply(chosen)
    off = switches.describe(chosen)
    if off:
        click.echo(click.style(
            f"optimisations OFF for this run: {', '.join(off)}", fg="yellow", bold=True))


def _configure_django(database_url=None):
    """Bring the app registry up. Touches no database."""
    import django
    if database_url:
        os.environ["OVERLAY_BENCH_DATABASE_URL"] = database_url
    # Set, not setdefault. benchmark/settings.py is not a default anybody would
    # want to override: it names the bench apps, the bench database and the four
    # optimisation switches. An exported DJANGO_SETTINGS_MODULE -- which is the
    # normal state of a Django developer's shell, and of a pytest run -- used to
    # win here, and then --no-optimisations moved a setting nothing read. Every
    # switch was inert and the run reported the default arm under the other
    # arm's name.
    os.environ["DJANGO_SETTINGS_MODULE"] = "benchmark.settings"
    root = str(_repository_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    django.setup()


def _setup_django(database_url):
    _configure_django(database_url)
    from django.core.management import call_command
    call_command("migrate", verbosity=0, interactive=False)


def _repository_root():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent


def _write_output(outcome, options, cap_ms):
    """Also emit markdown or json, for a CI step summary or a results file.

    The terminal has already seen its tables streamed section by section, so
    this is purely the second copy in another shape.
    """
    if options["output_format"] == "table" and not options["output"]:
        return

    if options["output_format"] == "json":
        text = json.dumps(outcome, indent=2, default=str)
    else:
        blocks = [
            f"## Benchmark: scale {options['scale']}, "
            f"{options['passes']} pass(es), {cap_ms // 1000}s cap",
            "",
            f"Ran in {_humanise(outcome['seconds'])} against a "
            f"{_humanise(parse_duration(options['max_runtime']))} ceiling.",
            "",
        ]
        if outcome["skipped"]:
            blocks += [f"**Skipped for budget:** {', '.join(outcome['skipped'])}", ""]
        if outcome.get("truncated"):
            blocks += [f"**Cut short for budget:** {', '.join(outcome['truncated'])}", ""]
        for suite in outcome["suites"]:
            if not suite["sections"]:
                continue
            blocks.append(f"### {suite['title']}")
            for section in suite["sections"]:
                blocks.append(_markdown_from_data(section, cap_ms))
        text = "\n".join(blocks)

    if options["output"]:
        with open(options["output"], "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        click.echo(text)


def _markdown_from_data(section, cap_ms):
    """Render a saved-shape section without rebuilding harness objects."""
    headers = ["query", *section["columns"]]
    lines = [f"**{section['title']}**", ""]
    if section["note"]:
        lines += [section["note"], ""]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in section["rows"]:
        cells = [row["label"]]
        for column in section["columns"]:
            cell = row["cells"].get(column)
            if cell is None:
                cells.append(row["extras"].get(column, ""))
            elif cell.get("note"):
                cells.append(cell["note"])
            elif cell["capped"]:
                cells.append(f">{cap_ms // 1000}s")
            else:
                cells.append(f"{cell['ms']:.0f}ms")
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def _humanise(seconds):
    from benchmark.harness import humanise
    return humanise(seconds)


if __name__ == "__main__":  # pragma: no cover
    benchmark()
