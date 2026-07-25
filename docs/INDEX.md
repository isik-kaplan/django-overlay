# django-overlay docs

- [USAGE.md](USAGE.md) — declaring an overlay model, basic ORM behavior
- [ARCHITECTURE.md](ARCHITECTURE.md) — the base table/view split, the three triggers
- [MANY_TO_MANY.md](MANY_TO_MANY.md) — why plain `ManyToManyField` is unsafe here, and the fix
- [IDS.md](IDS.md) — pk strategies, keeping organic and source ids from colliding
- [UNIQUENESS.md](UNIQUENESS.md) — `OverlayUniqueConstraint`, indexing, benchmarks
- [DELETION.md](DELETION.md) — delete semantics, `soft_delete`, `reset_to_source()`
- [MIGRATIONS.md](MIGRATIONS.md) — what `makemigrations` handles for you, and what it can't
- [LIMITATIONS.md](LIMITATIONS.md) — what's left to you, and unsupported `Meta` options
- [DEVELOPMENT.md](DEVELOPMENT.md) — running the tests
