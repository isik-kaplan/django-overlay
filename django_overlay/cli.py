"""The `django-overlay` command.

This is the only part of the command line that ships in the wheel. The
benchmark it can run does not: `benchmark/` exists in the repository and is
excluded from the built package, because it carries a Django app, a docker
compose file and a data generator that writes several million rows — none of
it any use to somebody who installed the library.

So the subcommand is resolved lazily. From a source checkout `benchmark` is a
full click command with two dozen options; from an installed wheel the import
fails and the user is told why, rather than being shown a traceback or a
command that silently does not exist.

Nothing here imports click. An earlier version built the whole group with it,
which meant `pip install django-overlay` had to pull click in for a command
that, in an installed copy, does nothing but explain its own absence — or else
`django-overlay --help` failed on the very installations the shim exists to be
polite to. Argument handling for one optional subcommand is small enough to do
by hand, and click stays where it belongs: a dependency of the benchmark.
"""

import sys


USAGE = """\
usage: django-overlay <command> [options]

commands:
  benchmark    Measure the overlay against plain tables (source checkout only).

  --version    Print the installed version.
  --help       Print this message.
"""

BENCHMARK_MISSING = """\
`django-overlay benchmark` needs a source checkout.

The benchmark suite is deliberately not shipped in the installed package: it
carries its own Django models, migrations and docker compose setup. To run it:

    git clone https://github.com/isik-kaplan/django-overlay
    cd django-overlay
    uv sync
    uv run django-overlay benchmark --help
"""


def load_benchmark():
    """The benchmark command, or None when it is not in this installation."""
    try:
        from benchmark.cli import benchmark
    except ImportError:
        return None
    return benchmark


def _version():
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("django-overlay")
    except PackageNotFoundError:  # pragma: no cover - only when run from a raw tree
        return "unknown"


def main(argv=None):
    """Entry point for the `django-overlay` script."""
    arguments = list(sys.argv[1:] if argv is None else argv)

    if not arguments or arguments[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0

    if arguments[0] in ("-V", "--version", "version"):
        sys.stdout.write(f"django-overlay {_version()}\n")
        return 0

    command, rest = arguments[0], arguments[1:]

    if command == "benchmark":
        benchmark = load_benchmark()
        if benchmark is None:
            sys.stderr.write(BENCHMARK_MISSING)
            return 1
        # A click command. standalone_mode=False keeps it from calling
        # sys.exit() itself, so this function stays the single place the exit
        # code comes from -- but it also stops click printing its own usage
        # errors, which would turn a mistyped flag into a traceback. So they
        # are caught and shown here instead.
        import click

        try:
            return benchmark(
                args=rest,
                # Otherwise click names itself after argv[0] and its usage line
                # reads `django-overlay [OPTIONS]`, which is not a command
                # anyone can type.
                prog_name="django-overlay benchmark",
                standalone_mode=False,
            ) or 0
        except click.ClickException as error:
            error.show()
            return error.exit_code
        except click.exceptions.Abort:
            sys.stderr.write("aborted\n")
            return 1

    sys.stderr.write(f"django-overlay: unknown command {command!r}\n\n{USAGE}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
