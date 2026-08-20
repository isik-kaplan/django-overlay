"""The shipped command line.

`django_overlay/cli.py` is the only part of the CLI that goes into the wheel,
and almost all of what it does is decide whether the benchmark is reachable.
Both answers matter: from a checkout the subcommand has to be the real one, and
from an installed copy it has to explain itself rather than produce a traceback
or a command that silently does not exist.

The other thing under test here is that none of this needs click. The shipped
package does not depend on it, so every path below has to work without it —
which is why the "missing" cases are checked for their message rather than for
an exception type click would have raised.
"""

import builtins
import sys
from importlib import metadata
from unittest import mock

import pytest

from django_overlay import cli


def without_benchmark():
    """Make `import benchmark.cli` fail, the way an installed copy would."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("benchmark"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    return mock.patch.object(builtins, "__import__", fake_import)


def test_no_arguments_prints_the_usage(capsys):
    assert cli.main([]) == 0
    assert "benchmark" in capsys.readouterr().out


# Every alias, not just the long one. `--help` alone passing says nothing about
# `-h`, and mutation testing found exactly that: breaking `-h` cost nothing.
@pytest.mark.parametrize("spelling", ["-h", "--help", "help"])
def test_every_spelling_of_help_prints_the_usage(capsys, spelling):
    assert cli.main([spelling]) == 0
    assert "usage: django-overlay" in capsys.readouterr().out


@pytest.mark.parametrize("spelling", ["-V", "--version", "version"])
def test_every_spelling_of_version_prints_the_version(capsys, spelling):
    assert cli.main([spelling]) == 0
    assert capsys.readouterr().out == f"django-overlay {metadata.version('django-overlay')}\n"


def test_the_version_printed_is_the_installed_one(capsys):
    """`django-overlay ` is a literal in the f-string, so matching it proves nothing.

    The assertion that used to be here checked the output contained
    "django-overlay", which is true whatever `_version()` returns -- including
    "unknown", which is what it returns when the package name it looks up is
    wrong. Two mutants lived in that gap.
    """
    assert cli.main(["--version"]) == 0
    printed = capsys.readouterr().out.split()[-1]
    assert printed == metadata.version("django-overlay")
    assert printed != "unknown"


def test_arguments_come_from_argv_after_the_program_name(monkeypatch, capsys):
    """Nothing called main() without an explicit argv, so the slice was free to be wrong.

    Asserting only that the output mentions "django-overlay" would not do it:
    the usage text says that too, so dropping an argument and printing usage
    instead would pass. It has to be the version line specifically.
    """
    monkeypatch.setattr(sys, "argv", ["django-overlay", "--version"])
    assert cli.main() == 0
    assert capsys.readouterr().out == f"django-overlay {metadata.version('django-overlay')}\n"


def test_an_unknown_command_is_an_error(capsys):
    assert cli.main(["nonsense"]) == 2
    captured = capsys.readouterr()
    assert "unknown command" in captured.err
    assert "usage: django-overlay" in captured.err


def test_the_benchmark_command_resolves_from_a_checkout():
    # This repository is a checkout, so the real command must come back.
    command = cli.load_benchmark()
    assert command is not None
    assert command.name == "benchmark"


def test_the_subcommand_is_the_real_one(capsys):
    assert cli.main(["benchmark", "--help"]) == 0
    assert "--scale" in capsys.readouterr().out


def test_an_installed_copy_explains_itself_instead_of_failing(capsys):
    with mock.patch.object(cli, "load_benchmark", return_value=None):
        assert cli.main(["benchmark"]) == 1
    assert "needs a source checkout" in capsys.readouterr().err


def test_the_usage_still_works_when_the_benchmark_is_absent(capsys):
    """An installed copy must still be able to print its own help."""
    with without_benchmark():
        assert cli.main(["--help"]) == 0
    assert "benchmark" in capsys.readouterr().out


def test_the_loader_returns_none_when_the_directory_is_absent():
    with mock.patch.dict(sys.modules), without_benchmark():
        sys.modules.pop("benchmark.cli", None)
        assert cli.load_benchmark() is None


def test_the_version_falls_back_when_the_package_is_not_installed():
    from importlib.metadata import PackageNotFoundError

    with mock.patch("importlib.metadata.version", side_effect=PackageNotFoundError):
        assert cli._version() == "unknown"


def test_a_mistyped_flag_is_a_usage_error_not_a_traceback(capsys):
    """standalone_mode=False stops click printing these, so the shim must."""
    assert cli.main(["benchmark", "--nonsense-flag"]) == 2
    assert "No such option" in capsys.readouterr().err


def test_a_usage_error_names_a_command_somebody_could_type(capsys):
    """Without prog_name, click names itself after argv[0] and suggests nonsense."""
    cli.main(["benchmark", "--nonsense-flag"])
    assert "Usage: django-overlay benchmark" in capsys.readouterr().err


def test_a_subcommand_that_returns_nothing_still_exits_zero():
    """click commands return None on success; the shim turns that into an exit code."""
    with mock.patch.object(cli, "load_benchmark") as loader:
        loader.return_value = mock.Mock(return_value=None)
        assert cli.main(["benchmark"]) == 0


def test_a_subcommand_that_returns_a_code_keeps_it():
    with mock.patch.object(cli, "load_benchmark") as loader:
        loader.return_value = mock.Mock(return_value=3)
        assert cli.main(["benchmark"]) == 3


def test_an_aborted_prompt_exits_quietly(capsys):
    import click

    with mock.patch.object(cli, "load_benchmark") as loader:
        loader.return_value = mock.Mock(side_effect=click.exceptions.Abort())
        assert cli.main(["benchmark"]) == 1
    assert capsys.readouterr().err == "aborted\n"
