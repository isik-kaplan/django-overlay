"""Shared Jinja env for sql_templates/. Split out of sql.py so
strategies.py can render templates too without a circular import."""

import jinja2


def _qi(name: str) -> str:
    """Jinja filter: quote a Postgres identifier, e.g. reserved words like `order`."""
    return '"' + name.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    """Jinja filter: quote a value as a SQL string literal. Only for names this
    package generates itself — it is not a substitute for bind parameters."""
    return "'" + str(value).replace("'", "''") + "'"


_ENV = jinja2.Environment(
    loader=jinja2.PackageLoader("django_overlay", "sql_templates"),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)
_ENV.filters["qi"] = _qi
_ENV.filters["sql_literal"] = _sql_literal

# The templates that exist, spelled the way they are actually spelled. Read
# once: this package's templates ship inside it and cannot appear at runtime.
_TEMPLATE_NAMES = frozenset(_ENV.list_templates())


def render(template_name: str, **context) -> str:
    """One of this package's SQL templates, rendered and stripped.

    The name is checked against the real list before Jinja sees it, because
    Jinja's loader resolves a template by opening the file and half the world's
    filesystems are case-insensitive. `render("view/view.sql.j2")` and
    `render("VIEW/VIEW.SQL.J2")` are the same call on a Mac and different calls
    on the Linux box that runs CI, which turns a plain typo into a bug that
    only reproduces on someone else's machine.

    It also makes the mutation suite mean the same thing everywhere. Uppercasing
    a string literal is one of the mutations mutmut generates, so every one of
    these call sites has a mutant that dies in CI and survives on a laptop --
    ten of them, at the last count, permanently in the local survivor list and
    permanently needing to be explained away by hand.
    """
    if template_name not in _TEMPLATE_NAMES:
        raise jinja2.TemplateNotFound(template_name)
    return _ENV.get_template(template_name).render(**context).strip()
