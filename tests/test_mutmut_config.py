"""[tool.mutmut] must only contain keys mutmut actually reads.

mutmut takes its configuration from pyproject.toml and ignores, silently, any
key it does not know. That is how `max_children = 1` sat in the config for
weeks doing nothing: it is a flag on `mutmut run`, never a config field, so
every run forked one child per CPU. Those children share one Postgres test
database, so they raced each other's schema, tests failed for reasons that had
nothing to do with the mutation, and mutmut recorded 2,209 mutants as killed
when a serial re-run of one module alone found eight survivors -- a green
report built entirely out of broken tests.

A typo in a key name fails exactly the same way: quietly, with a plausible
result. So rather than trusting the config to be right, ask mutmut which keys
it consults and compare.
"""

import tomllib
from pathlib import Path

import pytest
from mutmut import configuration


def _keys_mutmut_reads():
    """Every key mutmut looks for, observed rather than assumed.

    Wrapping mutmut's own config reader means the list comes from mutmut
    executing its own config loading, so it stays honest across upgrades rather
    than being a copy that goes quietly out of date.

    Two details are deliberate. The wrapper passes each lookup through to the
    real reader instead of answering with the default: answering with defaults
    leaves `source_paths` empty, mutmut falls back to guessing where the source
    is, and the guess raises inside `mutants/` -- which fails this test in the
    one place where a failing test costs a whole mutation run. And a raise from
    _load_config is tolerated, because the keys asked for before it are still
    the answer to the only question here; test_mutmut_reads_a_plausible_set_of_keys
    is what catches the case where nothing was observed at all.
    """
    seen = set()
    original = configuration._config_reader

    def recording_reader():
        real = original()

        def s(key, default):
            seen.add(key)
            return real(key, default)

        return s

    configuration._config_reader = recording_reader
    try:
        configuration._load_config()
    except Exception:  # pragma: no cover -- environment-dependent, see above
        pass
    finally:
        configuration._config_reader = original
    return seen


@pytest.fixture(scope="module")
def configured_keys():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    return set(pyproject["tool"]["mutmut"])


def test_mutmut_reads_a_plausible_set_of_keys():
    # Guards the guard: if the introspection above silently stopped observing
    # anything, every other assertion here would pass vacuously.
    keys = _keys_mutmut_reads()
    assert "source_paths" in keys
    assert "pytest_add_cli_args" in keys
    assert len(keys) > 10


def test_every_configured_key_is_one_mutmut_reads(configured_keys):
    ignored = configured_keys - _keys_mutmut_reads()
    assert not ignored, (
        f"[tool.mutmut] sets {sorted(ignored)}, which mutmut does not read. "
        "A key it does not know is discarded without a word -- check whether "
        "it is a `mutmut run` command-line flag instead."
    )


def test_max_children_is_not_configured_as_if_it_worked(configured_keys):
    # Named on its own because this is the one that actually happened, and the
    # failure it caused -- every mutant reported killed -- reads as success.
    assert "max_children" not in configured_keys, (
        "max_children is a flag on `mutmut run`, not a config key. Pass "
        "--max-children 1 on the command line; in the config it is ignored "
        "and mutmut forks one child per CPU onto a shared test database."
    )
