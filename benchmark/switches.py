"""The four query optimisations, in one table.

Three places have to agree about them and none of them can see the others: the
CLI turns each into a flag, `settings.py` turns each into a Django setting
before `django.setup()` reads it, and `environment.py` records which were on so
that a saved run says what arm it was measuring. The table lives here so that
those three read one set of names instead of three, and so that a test can
assert the CLI exposes exactly these -- a fifth optimisation is an entry here
and a click option there, and the test fails until it is both.

The library reads every one of them through `getattr(settings, name, True)`, so
absent means on. That is what makes the switches usable at all: the benchmark's
default arm is the configuration a real project gets, and turning one off is an
explicit act with a flag attached to it.

Nothing here imports Django or click. `settings.py` is imported *during*
`django.setup()`, so anything it reaches has to be importable before the app
registry exists.
"""

import os
from collections import namedtuple


# `flag` is the click flag without its leading dashes, and also the option's
# python name once click has underscored it -- one string, so the CLI and the
# table cannot drift apart on spelling. `setting` is both the Django setting
# and the environment variable, which are deliberately the same name: the only
# way settings.py can hear about a flag is through the environment, and giving
# the two ends different names would buy nothing but a translation table.
Switch = namedtuple("Switch", "flag setting help")

REWRITE_TRAVERSALS = Switch(
    "rewrite-traversals",
    "DJANGO_OVERLAY_REWRITE_TRAVERSALS",
    "Rewrite a filter that traverses between two overlay views into a subquery.",
)
REDIRECT_SELECT_RELATED = Switch(
    "redirect-select-related",
    "DJANGO_OVERLAY_REDIRECT_SELECT_RELATED",
    "Route select_related() across views through prefetch_related() instead.",
)
FORCE_HASH_JOINS = Switch(
    "force-hash-joins",
    "DJANGO_OVERLAY_FORCE_HASH_JOINS",
    "Ban nested loops for a query joining several overlay views.",
)
ARRAY_SUBQUERY_IN = Switch(
    "array-subquery-in",
    "DJANGO_OVERLAY_ARRAY_SUBQUERY_IN",
    "Fence an __in subquery as `lhs = ANY (ARRAY(subquery))`.",
)

SWITCHES = (
    REWRITE_TRAVERSALS,
    REDIRECT_SELECT_RELATED,
    FORCE_HASH_JOINS,
    ARRAY_SUBQUERY_IN,
)

# What counts as on when the value arrives as text. Anything else is off,
# including the empty string -- `DJANGO_OVERLAY_FORCE_HASH_JOINS=` in a shell
# script reads as "off", which is the safer of the two readings for a variable
# somebody meant to set and fumbled.
TRUTHY = frozenset({"1", "true", "yes", "on"})


def option_name(switch):
    """The name click gives the flag's value: `--force-hash-joins` -> force_hash_joins."""
    return switch.flag.replace("-", "_")


def read(switch, environ=None, default=True):
    """One switch as the environment currently has it.

    Returns a real bool, never a string: the library raises
    ImproperlyConfigured for anything that is not a bool, which is deliberate
    on its part and would otherwise fire here first, on our own settings
    module, and read as a bug in the library rather than in the harness.
    """
    raw = (os.environ if environ is None else environ).get(switch.setting)
    return default if raw is None else raw.strip().lower() in TRUTHY


def state(environ=None):
    """{option name: bool} for every switch, for the run's environment record."""
    return {option_name(switch): read(switch, environ) for switch in SWITCHES}


def apply(values, environ=None):
    """Write the chosen values where `settings.py` will read them.

    Must happen before `django.setup()`. It is the environment rather than a
    direct attribute set on the settings module because the CLI resolves the
    flags before Django exists -- there is no settings object to poke yet, and
    creating one early is how a project ends up with two of them.
    """
    target = os.environ if environ is None else environ
    for switch in SWITCHES:
        target[switch.setting] = "1" if values[option_name(switch)] else "0"


def resolve(options, all_off=False):
    """Turn the CLI's tri-state flags into a decision for each switch.

    Each flag is None until given, so `--no-optimisations` can move the floor
    under all four while an explicit `--force-hash-joins` still lifts one back
    out of it. That combination is the useful one: it is how you ask what a
    single optimisation is worth on its own, rather than what the four are
    worth together.
    """
    base = not all_off
    chosen = {}
    for switch in SWITCHES:
        name = option_name(switch)
        given = options.get(name)
        chosen[name] = base if given is None else given
    return chosen


def configured(source=None):
    """{option name: bool} as Django actually has it, for the run's record.

    Read off the settings object rather than the environment we wrote to. The
    environment is how the CLI asks; settings is what the library obeys. A
    record of what was asked for would hide the one bug worth catching here --
    a flag that never reached the library, which is what an A/B arm that quietly
    measured the default arm twice looks like from the outside.
    """
    if source is None:
        from django.conf import settings as source
    return {
        option_name(switch): bool(getattr(source, switch.setting, True))
        for switch in SWITCHES
    }


def describe(values):
    """The switches that are off, named the way they were typed on the command
    line rather than the way python spells them."""
    flags = {option_name(switch): switch.flag for switch in SWITCHES}
    return sorted(flags.get(name, name) for name, on in values.items() if not on)
