# Running the tests

Needs a real Postgres instance — no SQLite path.

```bash
uv sync
POSTGRES_USER=postgres uv run pytest
```

Covers `NEGATIVE_ID`/`UUID4`/`UUID7_POLYFILL` (native `UUID7` needs Postgres
18+, covered only by pure-Python/SQL-string unit tests) across a
People/Address/Phone model set: both M2M styles, a one-to-one and a plain FK
bonus relation, and the checks that reject unsafe FK/M2M usage.

`tests/test_tenants/` proves the same mechanism under `django_tenants`
schema-per-tenant multi-tenancy — separate settings module and database:

```bash
DJANGO_SETTINGS_MODULE=tests.django_tenants_settings POSTGRES_USER=postgres \
  uv run pytest tests/test_tenants -o addopts="" --create-db
```

The main suite (not `test_tenants`) is 100% line+branch coverage, enforced:

```bash
POSTGRES_USER=postgres uv run pytest --cov=django_overlay --cov-report=term-missing
```

Fails if coverage drops below 100% (`fail_under` in `pyproject.toml`).
