"""Shared Jinja env for sql_templates/. Split out of sql.py so
strategies.py can render templates too without a circular import."""

import jinja2


def _qi(name: str) -> str:
    """Jinja filter: quote a Postgres identifier, e.g. reserved words like `order`."""
    return '"' + name.replace('"', '""') + '"'


_ENV = jinja2.Environment(
    loader=jinja2.PackageLoader("django_overlay", "sql_templates"),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)
_ENV.filters["qi"] = _qi


def render(template_name: str, **context) -> str:
    return _ENV.get_template(template_name).render(**context).strip()
