# django-overlay docs

**Guide**
- [guide/USAGE.md](guide/USAGE.md) — declaring an overlay model, basic ORM behavior
- [guide/MANY_TO_MANY.md](guide/MANY_TO_MANY.md) — why plain `ManyToManyField` is unsafe here, and the fix

**Concepts**
- [concepts/ARCHITECTURE.md](concepts/ARCHITECTURE.md) — the base table/view split, the three triggers
- [concepts/IDS.md](concepts/IDS.md) — pk strategies, keeping organic and source ids from colliding
- [concepts/UNIQUENESS.md](concepts/UNIQUENESS.md) — `OverlayUniqueConstraint`, indexing, benchmarks
- [concepts/DELETION.md](concepts/DELETION.md) — delete semantics, `soft_delete`, `reset_to_source()`

**Reference**
- [reference/COMPATIBILITY.md](reference/COMPATIBILITY.md) — measured tables of what matches a plain Django model and what doesn't
- [reference/QUERY_REWRITING.md](reference/QUERY_REWRITING.md) — the three query shapes rewritten for you, why, and how to turn each off

**Operations**
- [operations/MIGRATIONS.md](operations/MIGRATIONS.md) — what `makemigrations` handles for you, and what it can't
- [operations/PERFORMANCE.md](operations/PERFORMANCE.md) — measured against a plain table, and the two source indexes `NEGATIVE_ID` needs
- [operations/LIMITATIONS.md](operations/LIMITATIONS.md) — what's left to you, and unsupported `Meta` options

**Development**
- [development/DEVELOPMENT.md](development/DEVELOPMENT.md) — running the tests
