"""Exposes the `qi` Jinja filter to sqlfluff's own Jinja env via
library_path. Not importing django_overlay here since pre-commit's
isolated hook env won't have it installed — keep this in sync with
_templating._qi by hand if it ever changes."""


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


SQLFLUFF_JINJA_FILTERS = {"qi": _qi}
