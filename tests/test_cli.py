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
from unittest import mock

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


def test_help_prints_the_usage(capsys):
    assert cli.main(["--help"]) == 0
    assert "usage: django-overlay" in capsys.readouterr().out


def test_version_prints_a_version(capsys):
    assert cli.main(["--version"]) == 0
    assert "django-overlay" in capsys.readouterr().out


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


def test_an_aborted_prompt_exits_quietly(capsys):
    import click

    with mock.patch.object(cli, "load_benchmark") as loader:
        loader.return_value = mock.Mock(side_effect=click.exceptions.Abort())
        assert cli.main(["benchmark"]) == 1
    assert "aborted" in capsys.readouterr().err
