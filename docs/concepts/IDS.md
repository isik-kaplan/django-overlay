# Ids: keeping the client side and the source side from colliding

An organic `Person.objects.create(...)` and an untouched source row must
never end up with the same id — otherwise the view's "not yet materialized"
check would treat them as the same row and one would silently hide the
other. `OverlayMeta.strategy` picks how; `OverlayMeta.with_strategy(...)`
sets it (plain `OverlayMeta` defaults to `Strategy.UUID4`).

Project-wide default: `settings.DJANGO_OVERLAY_DEFAULT_STRATEGY = Strategy.NEGATIVE_ID`
(read once at import time — set it in `settings.py`, not conditionally at
runtime). A model can still override with its own `.with_strategy(...)`.

- **`NEGATIVE_ID`** — integer pk. Source rows show up with their id
  *negated*; the base table's own sequence only hands out positive ids, so
  positive = organic, negative = source-derived, no coordination needed.
  `Person.objects.get(id=-42)` reaches source row 42. Only the source's own
  id is negated: any integer foreign key the source carries keeps its stored
  value, so it no longer names the row the view shows. Use it when the
  source's rows don't point at each other.
- **`NEGATIVE_SOURCE_POSITIVE_BASE_ID`** — integer pk, for a source that
  already stores its ids negative. Nothing is negated: source row -42 is
  `id=-42` in the view, the base table's sequence hands out positive ids as
  usual, and the source's own foreign keys still name the rows they point at.
- **`NEGATIVE_BASE_POSITIVE_SOURCE_ID`** — the mirror image, for a source
  whose ids are positive and that you'd rather not see negated. Source row 42
  stays `id=42`; the base table's sequence counts down from -1 instead, which
  the sync sets up on the existing sequence and keeps that way.

  Both trust you on the sign. Nothing checks that every source id really is
  on its side of zero, and one that isn't can collide with a base row. In
  exchange the anti-join compares a plain column on both sides, rather than
  `base.id = -source.id`, which only an expression index can serve.
- **`UUID4`** (default) — random uuid pk, no negation needed. Falls back to
  Postgres's `gen_random_uuid()` (13+) when an insert doesn't supply one.
- **`UUID7`** — time-ordered uuid (doesn't fragment a btree index like pure
  random v4 does). Falls back to Postgres 18's native `uuidv7()`. **Only use
  this on Postgres 18+**: PL/pgSQL resolves every function name at compile
  time, so a missing `uuidv7()` breaks *every* insert, not just id-less ones.
- **`UUID7_POLYFILL`** — same time-ordering, but the fallback is a portable
  SQL expression (`gen_random_uuid()` + `clock_timestamp()`, no extension
  needed). Use this unless you're sure you're on Postgres 18+.

The `id` field is auto-declared for any UUID strategy unless you declare
your own. The Postgres-side fallback only runs when nothing supplies an id
before the trigger sees it — the usual `default=uuid.uuid4` setup means
Django assigns it first, so the SQL fallback is really just there for raw
SQL/bulk paths. Override it directly with `OverlayMeta.pk_default_sql` (raw
SQL) if you want a specific generator regardless of strategy.
