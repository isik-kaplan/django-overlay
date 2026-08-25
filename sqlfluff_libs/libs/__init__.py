"""Exposes django_overlay's Jinja filters to sqlfluff's own Jinja env via
library_path. Deliberately does not import django_overlay: pre-commit runs
sqlfluff in an isolated environment that has no project install, and sqlfluff
imports everything it finds beside library_path -- which is why this sits one
level down, in libs/, rather than at the top of sqlfluff_libs/. Pointed at a
directory that sits next to django_overlay/, sqlfluff walks and imports the
package itself, and the linter dies on Django's "settings are not configured"
before it renders a single template.

So these are copies, and they have to be kept in sync with _templating.py by
hand. They are three lines each and have not changed since they were written;
if that stops being true, the honest fix is to import them, and to make the
hook run in an environment where that is possible.
"""


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


SQLFLUFF_JINJA_FILTERS = {"qi": _qi, "sql_literal": _sql_literal}
