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


def render(template_name: str, **context) -> str:
    return _ENV.get_template(template_name).render(**context).strip()
