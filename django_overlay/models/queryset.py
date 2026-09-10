"""OverlayQuerySet, and the manager Django installs from it.

Where the ORM surface is made to conform: count() decomposed into two counts,
select_related() routed to prefetch_related(), and an update that reads its own
columns routed around the view so Postgres can lock the row.
"""

import contextvars

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import connections, models, transaction
from django.db.models.constants import LOOKUP_SEP
from django.db.models.expressions import OuterRef, ResolvedOuterRef
from django.db.models.fields.reverse_related import ManyToManyRel, ManyToOneRel, OneToOneRel
from django.db.models.lookups import Lookup
from django.db.models.sql import Query

from .. import sql as overlay_sql
from .._templating import _qi
from ..exceptions import OverlayConfigurationError
from ..fields import OverlaySelectiveIn
from ..strategies import negates_source_ids
from .query import OverlayQuery


# Django's own update_or_create() locks the row it found with
# select_for_update() before updating it. That lock does nothing on a view, but
# neither does it hurt, and refusing it would break a core ORM method over a
# lock that was never real in the first place. This lets Django's internal use
# through while a direct call from application code still raises.
_django_internal_lock = contextvars.ContextVar("django_overlay_internal_lock", default=False)


def _reads_own_columns(value, model) -> bool:
    """Does this update value compute from a column of the row it's updating?

    That's the shape that can't survive going through the view: the scan reads
    the old row, Postgres folds the expression into a literal, and the trigger
    writes that literal — so a concurrent writer's change is overwritten rather
    than built on. A plain value or an expression over some other table is
    unaffected."""
    if isinstance(value, models.F):
        return value.name in {field.name for field in model._meta.concrete_fields}
    source_expressions = getattr(value, "get_source_expressions", None)
    if source_expressions is None:
        return False
    return any(_reads_own_columns(expression, model) for expression in source_expressions())


def _late_row_lookup_enabled() -> bool:
    """settings.DJANGO_OVERLAY_LATE_ROW_LOOKUP turns the ordered-page rewrite
    off. On by default: an unscoped ordered page measured 335ms against 40ms
    rewritten at 1,000,000 view rows, and it is the shape every list endpoint
    emits."""
    configured = getattr(settings, "DJANGO_OVERLAY_LATE_ROW_LOOKUP", True)
    if not isinstance(configured, bool):
        raise ImproperlyConfigured(f"settings.DJANGO_OVERLAY_LATE_ROW_LOOKUP must be a bool, got {configured!r}.")
    return configured


def _multiplies_rows(query) -> bool:
    """Can any join in this query return one model row more than once?

    The late-row-lookup rewrite fetches the page by primary key, and fetching
    by key returns each row once. So a query whose joins multiply rows must be
    left alone: the direct form returns a person once per matching address and
    the rewrite would return them once, which is fewer rows rather than the
    same rows faster.

    Read off the join descriptors, not the data. A forward `ForeignKey` or
    `OneToOneField` reaches at most one row on the far side, and so does a
    reverse one-to-one. A reverse foreign key or either direction of a
    many-to-many does not.

    `OneToOneRel` subclasses `ManyToOneRel`, so the order of these checks is
    load-bearing: test the narrow case first or every reverse one-to-one is
    refused for no reason. `ManyToManyRel` is *not* a `ManyToOneRel` subclass
    and needs naming separately -- the reverse m2m is the shape
    `prefetch_related()` emits, so missing it would refuse the common case
    while catching the rare one.
    """
    for join in query.alias_map.values():
        join_field = getattr(join, "join_field", None)
        if join_field is None:  # the FROM table carries no join field
            continue
        if isinstance(join_field, OneToOneRel):
            continue
        if isinstance(join_field, (ManyToOneRel, ManyToManyRel)):
            return True
    return False


def _refers_outward(expression) -> bool:
    """Does this expression tree reach for a column of an enclosing query?

    `Other.objects.filter(owner=OuterRef("pk")).order_by("id")[:1]` is a page
    like any other, but `OuterRef("pk")` names whatever query directly encloses
    it. Rewritten, the key query is enclosed by the page query rather than by
    the caller's, so the reference silently binds one level too shallow -- to
    the page's own primary key -- and the subquery returns other rows' values
    without raising. In an export annotating each row with its earliest
    related row this way, every row came back and the annotations were wrong.

    A nested query is not descended into. Its outer references point at *this*
    query, which the rewrite keeps intact in both the key query and the page,
    so `filter(Exists(...OuterRef("pk")))` is still a page worth rewriting.

    A lookup's right-hand side is read directly as well: Django lists it among
    the source expressions only when it compiles, and the unresolved
    `OuterRef` that `OuterRef(OuterRef(...))` leaves behind does not.
    """
    if isinstance(expression, (OuterRef, ResolvedOuterRef)):
        return True
    if isinstance(expression, Query):
        return False
    children = list(getattr(expression, "get_source_expressions", list)())
    if isinstance(expression, Lookup):
        children.append(expression.rhs)
    return any(_refers_outward(child) for child in children)


def _local_ordering(query) -> tuple | None:
    """The query's ORDER BY terms if every one is a plain local column, else None.

    The rewrite drops the page's `LIMIT` onto an inner query and puts the
    ordering back on the outer one, so the outer has to be able to express it.
    `order_by("addresses__city")` cannot survive that: the outer query is
    restricted by primary key and has no join to order across.

    Expressions are refused wholesale rather than inspected. An `F()` or a
    `Func` could be local and could equally wrap a traversal, and a rewrite
    that is right about most expressions is worse here than one that declines
    them all -- the failure it would produce is a differently-ordered page,
    which no exception marks.

    `Meta.ordering` counts, when nothing has overridden it. The SQL carries an
    ORDER BY either way, so refusing it would skip the rewrite for every model
    that sets a default order -- which is most of them.
    """
    terms = query.order_by or (query.get_meta().ordering if query.default_ordering else ())
    if not terms:
        return None
    local = {field.name for field in query.model._meta.concrete_fields}
    local |= {field.attname for field in query.model._meta.concrete_fields}
    for term in terms:
        if not isinstance(term, str):
            return None
        name = term.removeprefix("-")
        if name == "pk":
            continue
        if LOOKUP_SEP in name or name not in local:
            return None
    return tuple(terms)


def _redirect_select_related_enabled() -> bool:
    """settings.DJANGO_OVERLAY_REDIRECT_SELECT_RELATED turns the
    select_related -> prefetch_related routing off. On by default: a join
    between two overlay views measured 76x-1304x a plain table, and
    select_related() is the idiom every Django developer reaches for."""
    configured = getattr(settings, "DJANGO_OVERLAY_REDIRECT_SELECT_RELATED", True)
    if not isinstance(configured, bool):
        raise ImproperlyConfigured(
            f"settings.DJANGO_OVERLAY_REDIRECT_SELECT_RELATED must be a bool, got {configured!r}."
        )
    return configured


class OverlayQuerySet(models.QuerySet):
    """Default queryset for every overlay view model."""

    def __init__(self, model=None, query=None, using=None, hints=None):
        # OverlayQuery is what rewrites cross-view traversals; installing it
        # here rather than at each call site is also what makes the rewrite
        # recursive, since the subquery it builds is an OverlayQuerySet too.
        if query is None and model is not None:
            query = OverlayQuery(model)
        super().__init__(model=model, query=query, using=using, hints=hints)
        self._overlay_redirected = ()

    # ---------------------------------------------- which branch a row is from

    def with_origin(self, alias: str = "overlay_origin"):
        """Annotate each row with the branch it came from: "base" or "source".

        Free. The column is a literal in each branch of the view's UNION ALL, so
        reading it costs a constant per row and nothing else -- no join, no
        lookup against the source table.
        """
        return self.annotate(**{alias: self._origin_expression()})

    def base_only(self):
        """Only rows the tenant's own table holds -- edited or created here.

        Cheaper than not filtering at all, which is the point. The origin column
        is a constant per branch, so Postgres can prove the source branch
        contributes nothing and drop it: the Append node goes, and with it the
        anti-join the docs call the dominant cost of the whole view. What is
        left is a plain scan of the base table.

        That also takes the query out of the overlay's cost model entirely.
        There is no appendrel left to mis-estimate, so nothing here needs the
        nested-loop ban, the m2m fence or the traversal rewrite.
        """
        return self._filter_origin(overlay_sql.ORIGIN_BASE)

    def source_only(self):
        """Only vendor rows the tenant has never touched.

        The mirror image: the base branch is pruned instead, leaving the source
        scan and its anti-join.
        """
        return self._filter_origin(overlay_sql.ORIGIN_SOURCE)

    def overridden(self):
        """Base rows that shadow a source row -- vendor records edited here.

        Costs one semi-join against the source table on top of `base_only()`.
        That join is why the view does not carry a four-way origin column: as a
        literal per branch the origin prunes, and as anything computed it stops
        pruning for everybody. Paid here, by the caller who asked for it.
        """
        return self.base_only().filter(self._shadow_predicate())

    def organic(self):
        """Base rows with no source counterpart -- created here, not the
        vendor's. `base_only()` is `organic()` plus `overridden()`."""
        return self.base_only().exclude(self._shadow_predicate())

    # -- the machinery under those five

    def _origin_column(self) -> str:
        """The view's origin column, qualified.

        Raw rather than a model field, deliberately. A field would be a column
        Django puts in every INSERT, and the view is written through INSTEAD OF
        triggers whose column list comes from model._meta.fields -- so a field
        here would have to be excluded from writes in several places. An
        expression needs none of that: nothing changes about inserts, updates,
        or the triggers.
        """
        return f"{_qi(self.model._meta.db_table)}.{_qi(overlay_sql.ORIGIN_COLUMN)}"

    def _origin_expression(self):
        """The column as something annotate() can read back into Python.

        No output_field, deliberately, and not an oversight to correct: RawSQL
        substitutes a bare Field() for a missing one, and Django registers no
        lookup or transform that a TextField would have and a bare Field would
        not. Declaring the type was accurate and changed nothing -- two mutants
        said so, by removing it with every test still passing. The BooleanField
        below is the opposite case and has to stay.
        """
        return models.expressions.RawSQL(self._origin_column(), ())

    # The two below are conditions handed straight to filter()/exclude(), not
    # annotations compared to a value. Annotating needs an alias, and an alias
    # nothing ever reads is a name any change to which is unobservable -- four
    # surviving mutants worth, every one of them a rename. Without the alias
    # they cannot exist.
    #
    # It is also what makes the output_field below load-bearing: filter()
    # refuses an expression that is not conditional, and an expression is only
    # conditional when its output_field is a BooleanField. Drop it and this
    # raises, where on an annotation it would have gone unnoticed.

    def _filter_origin(self, value):
        return self.filter(
            models.expressions.RawSQL(f"{self._origin_column()} = %s", (value,), output_field=models.BooleanField())
        )

    def _shadow_predicate(self):
        """EXISTS() against the source row a base row would be shadowing.

        The two callers above take it from here rather than passing a flag
        down. A flag would be read for its truthiness only, which makes every
        other falsey value the same code -- `shadowing=False` and
        `shadowing=None` being indistinguishable is a surviving mutant, and an
        accurate one.

        No sourceless case to answer for: an overlay model without a source is
        refused at declaration time.
        """
        source = self.model.get_source()
        table = _qi(self.model._meta.db_table)
        pk = _qi(self.model._meta.pk.column)
        sign = "-" if negates_source_ids(self.model._overlay_meta.strategy) else ""
        return models.expressions.RawSQL(
            f"EXISTS (SELECT 1 FROM {_qi(source.schema)}.{_qi(source.table)} "
            f"WHERE {_qi(source.table)}.{_qi(source.id_column)} = {sign}{table}.{pk})",
            (),
            output_field=models.BooleanField(),
        )

    # ----------------------------------------------------- the selectivity lever

    def overlay_selective(self):
        """Declare this queryset's scope small, so nested loops stay allowed.

        The nested-loop ban in `planning` is decided by shape — how many
        overlay views land in one statement — because shape is the only thing
        readable off the code. What shape cannot see is *selectivity*, and
        selectivity is what decides whether the banned plan was wrong or right.

        Both cases are already measured, from opposite sides:

        - Broad, and the ban is load-bearing. Two m2m hops at 1,000,000 people
          are 752ms banned and do not finish unbanned; three are 3,851ms.
        - Selective, and the ban is the regression. `_HASH_JOIN_THRESHOLD`'s own
          note records 7ms -> 60ms for a selective filter with no LIMIT, because
          "200 rows is exactly where a nested loop is the right plan".

        An identity search — name plus email plus phone, resolving to one person
        — is three hops and therefore five views, so it trips the threshold
        unconditionally, and a forced hash build over the whole email relation
        is what it costs. That is the shape this exists for::

            Person.objects.filter(
                state__in=states, name=name,
                emails__address=email, phones__number=phone,
            ).overlay_selective()

        Use it when you know the predicate resolves to a handful of rows: a
        unique-ish column, an id, an exact contact detail. Do not use it on a
        scope you cannot bound — a state, a city, a status flag — because the
        plan it re-permits is the one that runs to exhaustion, and there is no
        LIMIT on the damage.

        Nothing here probes the database to decide. That is the same line the
        m2m fence holds and the reason the decision is yours: SQL that depends
        on database state is SQL you cannot read off the code.

        **Not the same mechanism as `pk__overlay_selective_in`**, which is the
        other half of that reasoning and shares only the claim. This method
        suppresses `SET enable_nestloop = off` for the whole statement, so it
        moves every join in it. That lookup changes how one `IN` compiles —
        `= ANY (ARRAY(subquery))` instead of a semi-join — and touches nothing
        else. See `OverlaySelectiveIn` for the side-by-side.

        The failure modes differ more than the mechanisms do, and that is the
        part worth remembering. A wrong claim there costs a bounded factor: a
        plain `IN` overtakes the array form somewhere above 5% of the table and
        is ~2-3x better by 50%. A wrong claim *here* re-permits the plan that
        runs to exhaustion, with no crossover where it merely stops paying.

        Reachable from a subquery, since the view count is too — see
        `planning._selective_declared()`. Chainable and position-independent;
        the flag is read once, at compile time.
        """
        clone = self._chain()
        clone.query._overlay_selective = True
        return clone

    # -------------------------------------------------- the ordered-page rewrite

    def overlay_direct(self):
        """Take the ordered page as one statement, without the key round trip.

        Opts out of the late-row-lookup rewrite for this queryset. Unlike the
        two selectivity levers this is not a claim about your data that the
        library cannot see -- the rewrite is decided entirely by shape, and it
        was measured winning at every page size from 20 to 100,000 rows. It
        exists for the one case that shape genuinely cannot distinguish: a
        slice that is the whole table rather than a page of it, where the
        direct form measured 289ms against 746ms rewritten.

        If you find yourself reaching for it on an actual page, that is worth
        reporting rather than working around.
        """
        clone = self._chain()
        clone.query._overlay_direct = True
        return clone

    def __getitem__(self, k):
        """Resolve an ordered page's keys first, then fetch those rows by key.

        `ORDER BY … LIMIT 50` over the view sorts whole rows to keep fifty of
        them. The sort itself is not the cost -- it is a top-N heapsort holding
        31kB -- but the rows arrive unordered, so every one of them must be
        produced and passed through it. At 1,000,000 view rows that measured
        335ms, of which the projection is most: the same page selecting only the
        primary key is 40ms, and one extra column takes it back to 234ms.

        So resolve the keys and fetch the rows separately::

            SELECT * FROM person_view
            WHERE id = ANY (ARRAY(SELECT id FROM person_view ORDER BY id LIMIT 50))
            ORDER BY id

        335ms -> 40ms, one statement, identical rows. It wins at every page size
        measured (x9.0 at 20, x8.5 at 200, x7.3 at 5,000, x3.4 at 100,000) and
        only loses when the slice *is* the whole table, which `overlay_direct()`
        is for.

        There is no cap on the page size and deliberately so. The array holds
        one id per row the caller has already asked to receive, and an id is
        narrower than the row it stands for -- so unlike an unfenced subquery,
        this cannot materialise more than the answer itself. The pathological
        slice is expensive, not unbounded.

        Everything the rewrite needs is in the query's shape; nothing here
        reads the database. What it refuses is in `_wants_late_row_lookup()`.
        """
        if isinstance(k, slice) and self._wants_late_row_lookup(k):
            # `.values("pk")` before the slice, not after: Django refuses to
            # narrow a queryset that has already been sliced. This is also what
            # stops the rewrite recursing -- the inner queryset selects the
            # primary key and nothing else, which is the one shape
            # `_wants_late_row_lookup()` declines.
            keys = self.values("pk")[k]
            # `filter()` on self, rather than a fresh queryset carrying self's
            # output configuration across by hand. Deferred fields, prefetches,
            # annotations and the iterable class all stay attached, and the
            # original predicate stays too -- redundant, since those keys
            # already satisfied it, and harmless for the same reason. Any join
            # it brought is now evaluated against fifty keys instead of the
            # whole view.
            page = self.filter(**{f"pk__{OverlaySelectiveIn.lookup_name}": keys})
            # The statement still carries a LIMIT; it moved into the array
            # subquery. Say so, or the nested-loop ban's lowered threshold --
            # which triggers on a query that carries a slice -- stops seeing
            # this one. See OverlayQuery._wants_hash_joins().
            page.query._overlay_paged = True
            return page
        return super().__getitem__(k)

    def _wants_late_row_lookup(self, k: slice) -> bool:
        """Whether `self[k]` should become a key lookup. Shape only.

        Every clause below is a refusal, and each one is a way the rewrite
        would otherwise return different rows rather than the same rows faster:

        - **A step.** `qs[::2]` is handed to Python, not to SQL.
        - **An unbounded slice.** `qs[10:]` has no `LIMIT`, so there is no
          bounded key set to fetch by and nothing to win.
        - **A model whose view has no full anti-join.** `overridable = False`
          leaves a `Merge Append` that `LIMIT` already stops early -- an
          unscoped ordered page on that shape measured 0.4ms against 311ms for
          the overridable one. There is nothing here to fix.
        - **No local ordering.** See `_local_ordering()`.
        - **A multiplying join.** See `_multiplies_rows()`. `distinct()`
          rescues it, because then both forms return each row once.
        - **A reference to an enclosing query.** A correlated subquery's
          `OuterRef` would bind to the rewritten page instead of the caller.
          See `_refers_outward()`.
        - **A primary-key-only projection.** Already the inner query of this
          rewrite; rewriting it again would recurse.
        - **A combinator, `extra()`, or a raw `values_select` beyond the pk.**
          `union()` cannot carry the outer filter, and `extra()` can select
          anything at all.
        """
        if k.step is not None or k.stop is None:
            return False
        if not _late_row_lookup_enabled():
            return False
        # A queryset handed a plain sql.Query cannot resolve the array lookup the
        # page is fetched by, and would raise FieldError on every ordered slice.
        if not isinstance(self.query, OverlayQuery) or self.query._overlay_direct:
            return False
        # Only an overridable view carries the full anti-join (see
        # overlay_sql.anti_join_kind()); without it LIMIT already stops early.
        if not self.model._overlay_meta.overridable:
            return False
        query = self.query
        if query.combinator or query.extra or query.is_sliced:
            return False
        if _refers_outward(query.where) or any(_refers_outward(a) for a in query.annotations.values()):
            return False
        if _local_ordering(query) is None:
            return False
        if _multiplies_rows(query) and not query.distinct:
            return False
        pk_names = {"pk", self.model._meta.pk.name, self.model._meta.pk.attname}
        return not (query.values_select and set(query.values_select) <= pk_names)

    # ------------------------------------------------ select_related routing

    def _clone(self):
        clone = super()._clone()
        clone._overlay_redirected = self._overlay_redirected
        return clone

    def select_related(self, *fields):
        """Route overlay paths to `prefetch_related()`, keep the rest as joins.

        `select_related('person')` compiles to a join between two views, and
        both sides are appendrels the planner has no statistics for. On the
        production-shaped graph a detail page that way is 15.2ms against a
        0.2ms plain-table baseline; the same data fetched as two queries with
        a literal id list — which is exactly what `prefetch_related()` emits —
        is 0.1ms. 152x, for the identical result.

        Nothing about attribute access changes. A forward FK prefetch leaves
        `link.person` a single instance, populated from a cache rather than
        from a join, and `None` where the FK is null. The trade is one extra
        query per redirected path, and that the two queries are two snapshots
        rather than one — inside a transaction, or against read-mostly vendor
        data, that is not observable.

        `select_related(None)` still clears, plain (non-overlay) targets still
        join, and `DJANGO_OVERLAY_REDIRECT_SELECT_RELATED = False` turns the
        whole thing off.
        """
        if not _redirect_select_related_enabled():
            return super().select_related(*fields)
        if fields == (None,):
            # Clearing has to undo the routing too, or the prefetch it stands
            # in for outlives the select_related() it replaced.
            clone = super().select_related(None)
            clone._prefetch_related_lookups = tuple(
                lookup for lookup in clone._prefetch_related_lookups if lookup not in self._overlay_redirected
            )
            clone._overlay_redirected = ()
            return clone
        if self.query.combinator:
            raise OverlayConfigurationError(
                f"select_related() after .{self.query.combinator}() cannot be routed around the "
                f"view, and joining two overlay views directly measured 76x-1304x a plain table. "
                f"Fetch the related rows in a second query instead."
            )

        overlay, plain = self._split_select_related(fields)
        clone = super().select_related(*plain) if plain else self._chain()
        if overlay:
            clone = clone.prefetch_related(*overlay)
            clone._overlay_redirected = tuple(self._overlay_redirected) + tuple(overlay)
        return clone

    def _split_select_related(self, fields):
        """(paths to prefetch, paths to leave as joins)."""
        if not fields:
            # Bare select_related() means "every forward relation". Naming them
            # explicitly is the only way to join some and prefetch others.
            fields = [
                field.name
                for field in self.model._meta.get_fields()
                if field.concrete and (field.many_to_one or field.one_to_one)
            ]
        overlay, plain = [], []
        for path in fields:
            head = path.split(LOOKUP_SEP)[0]
            try:
                field = self.model._meta.get_field(head)
            except FieldDoesNotExist:
                plain.append(path)
                continue
            target = getattr(field, "related_model", None)
            if target is not None and getattr(target, "_is_overlay_view_model", False):
                overlay.append(path)
            else:
                plain.append(path)
        return overlay, plain

    def _refuse_after_redirect(self, what, remedy):
        raise OverlayConfigurationError(
            f"{what} cannot be combined with select_related() on an overlay model. "
            f"select_related() is routed to prefetch_related() here, because joining two "
            f"overlay views measured 76x-1304x a plain table. {remedy}"
        )

    def iterator(self, chunk_size=None):
        if self._overlay_redirected and chunk_size is None:
            self._refuse_after_redirect(
                "iterator() without chunk_size",
                "Pass a chunk_size, or drop the select_related() and let the prefetch happen on the whole queryset.",
            )
        return super().iterator(chunk_size=chunk_size)

    def _values(self, *fields, **expressions):
        # select_related() is a no-op under values() in Django too, so dropping
        # the prefetch here is what "leave it alone" means -- not a change.
        clone = super()._values(*fields, **expressions)
        if self._overlay_redirected:
            clone._prefetch_related_lookups = tuple(
                lookup for lookup in clone._prefetch_related_lookups if lookup not in self._overlay_redirected
            )
            clone._overlay_redirected = ()
        return clone

    def union(self, *other_qs, all=False):
        if self._overlay_redirected:
            self._refuse_after_redirect("union()", "Fetch the related rows in a second query.")
        return super().union(*other_qs, all=all)

    def intersection(self, *other_qs):
        if self._overlay_redirected:
            self._refuse_after_redirect("intersection()", "Fetch the related rows in a second query.")
        return super().intersection(*other_qs)

    def difference(self, *other_qs):
        if self._overlay_redirected:
            self._refuse_after_redirect("difference()", "Fetch the related rows in a second query.")
        return super().difference(*other_qs)

    def count(self):
        """Count the two branches separately instead of counting the view.

        Postgres does not push an aggregate down through `UNION ALL`. So
        `count(*)` on the view builds the whole Append — every row of the base
        table, plus every source row that survives the anti-join — and counts
        what comes out one row at a time. Counting each branch on its own lets
        the base side become an index-only scan and lets each branch aggregate
        independently.

        Measured on a 3,000,000-row view: 639ms through the ORM, 560ms for
        `count(*)` on the view, 301ms decomposed, 261ms decomposed with a
        partial index on `(id) WHERE NOT _overlay_deleted`. That takes count()
        from ~13x a plain table to ~5x.

        This is a query rewrite, not a different answer: the SQL below is the
        view's own definition with `count(*)` in place of the select list, so
        it counts exactly the rows the view would have returned.

        Only a bare count qualifies. Anything with a predicate, a slice, an
        annotation, a combinator or `distinct()` falls through to Django."""
        if self._result_cache is None and self._can_decompose_count():
            return self._decomposed_count()
        return super().count()

    def _can_decompose_count(self) -> bool:
        """The decomposition counts whole tables, so anything that narrows,
        widens or reshapes the row set disqualifies it.

        `.values(...)` deliberately doesn't: `.values('age').count()` is still
        a count of every row, and only becomes a real dedup with distinct(),
        which is excluded here anyway."""
        query = self.query
        return (
            self.model.get_source() is not None
            and not query.is_sliced  # LIMIT/OFFSET caps the answer
            and not query.where  # any predicate at all
            and not query.annotations
            and not query.distinct
            and not query.combinator  # union()/intersection()/difference()
            and not query.group_by
            and not query.extra
            and len(query.alias_map) <= 1  # a join can multiply rows
        )

    def _decomposed_count(self) -> int:
        """`count(base WHERE NOT deleted) + count(source WHERE NOT EXISTS ...)`

        Not the tempting fully index-only form,
        `count(base) + count(source) - count(overridden)`. That one is wrong:
        if the vendor drops a row a tenant had already overridden, the override
        is orphaned — it still counts in `count(base)` but no longer matches
        anything to subtract, so the total undercounts. The `NOT EXISTS` stays.

        The base table is left unqualified so `search_path` resolves it to the
        current tenant's schema, which is what every other query in this
        library relies on; the source table carries its schema explicitly, as
        it does in the view."""
        model = self.model
        source = model.get_source()
        overlay_meta = model._overlay_meta
        connection = connections[self.db]
        quote = connection.ops.quote_name

        base_table = quote(model._base_model._meta.db_table)
        pk_column = quote(model._meta.pk.column)
        source_table = f"{quote(source.schema)}.{quote(source.table)}"
        source_id = f"{quote(source.table)}.{quote(source.id_column)}"
        if negates_source_ids(overlay_meta.strategy):
            source_id = f"-{source_id}"

        base_where = f" WHERE NOT {quote('_overlay_deleted')}" if overlay_meta.soft_delete else ""
        # Spliced raw, exactly as the view does it — see SourceTable.extra_where
        # for whose job the quoting is.
        extra_where = f"{source.extra_where} AND " if source.extra_where else ""

        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT (SELECT count(*) FROM {base_table}{base_where})"  # noqa: S608 - identifiers from _meta
                f" + (SELECT count(*) FROM {source_table} WHERE {extra_where}"
                f"NOT EXISTS (SELECT 1 FROM {base_table} AS overlay_base"
                f" WHERE overlay_base.{pk_column} = {source_id}))"
            )
            return cursor.fetchone()[0]

    def update(self, **kwargs):
        """Route a self-referencing update around the view.

        `update(age=F("age") + 1)` is atomic on a real table because Postgres
        re-evaluates the expression after taking the row lock. Through the view
        there is nothing left to re-evaluate — the expression has already been
        folded into a literal by the time the INSTEAD OF trigger runs — so
        concurrent increments are lost.

        The fix is to get out of Postgres's way rather than reimplement it:
        copy the matched rows into the base table, then run the update against
        that table, where the ordinary row-locking semantics apply. Measured at
        160/160 concurrent increments, against 148/160 through the view.

        Everything else keeps the single-statement path through the view."""
        if not any(_reads_own_columns(value, self.model) for value in kwargs.values()):
            return super().update(**kwargs)

        base_manager = self.model._base_model._default_manager
        with transaction.atomic(using=self.db):
            self._copy_matched_rows_to_the_base_table()
            return base_manager.using(self.db).filter(pk__in=self.values("pk")).update(**kwargs)

    def _update(self, values, returning_fields=None):
        """The same routing for `instance.save()`.

        Django's save() doesn't call update() — it calls the private _update()
        with a list of (field, model, value) — so intercepting update() alone
        left `obj.field = F("field") + 1; obj.save()` losing concurrent
        updates. Measured at 109/160 before this, 160/160 after.

        bulk_update() needs nothing extra: it builds a Case/When and hands it
        to update(), so it comes through the public path already.

        returning_fields is how save() gets the resolved number back to put on
        the instance in place of the expression. Django only ever asks for it
        when a value is an expression — which is exactly when we route — but
        the count-returning form is part of the QuerySet contract, so it is
        honoured rather than assumed away."""
        if not any(_reads_own_columns(value, self.model) for _, _, value in values):
            return super()._update(values, returning_fields)

        base_manager = self.model._base_model._default_manager
        with transaction.atomic(using=self.db):
            self._copy_matched_rows_to_the_base_table()
            matched = base_manager.using(self.db).filter(pk__in=self.values("pk"))
            updated = matched.update(**{field.name: value for field, _, value in values})
            if not returning_fields:
                return updated
            # Read the values back afterwards rather than with RETURNING, which
            # the copy-then-update pair can't carry across. Matching nothing
            # yields no rows, which is the falsy result save() expects.
            return list(
                base_manager.using(self.db)
                .filter(pk__in=self.values("pk"))
                .values_list(*[field.name for field in returning_fields])
            )

    def _copy_matched_rows_to_the_base_table(self) -> None:
        """Materialise every row this queryset matches, so the update that
        follows finds a base row for each of them. A row already materialised
        is left exactly as it is."""
        fields = list(self.model._meta.concrete_fields)
        names = [field.name for field in fields]
        columns = [field.column for field in fields]

        selection = self.order_by()
        if self.model._overlay_meta.soft_delete:
            # Base-only column, so it isn't in the view to copy across. A
            # matched row is by definition visible, hence not tombstoned.
            selection = selection.annotate(_overlay_not_deleted=models.Value(False, output_field=models.BooleanField()))
            names = [*names, "_overlay_not_deleted"]
            columns = [*columns, "_overlay_deleted"]

        connection = connections[self.db]
        select_sql, params = selection.values(*names).query.get_compiler(self.db).as_sql()
        quoted = ", ".join(connection.ops.quote_name(column) for column in columns)
        base_table = connection.ops.quote_name(self.model._base_model._meta.db_table)
        pk_column = connection.ops.quote_name(self.model._meta.pk.column)
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {base_table} ({quoted}) {select_sql} "  # noqa: S608 - identifiers from _meta
                f"ON CONFLICT ({pk_column}) DO NOTHING",
                params,
            )

    def update_or_create(self, *args, **kwargs):
        # aupdate_or_create() needs no equivalent: Django implements it as
        # sync_to_async(self.update_or_create), so it comes back through here.
        token = _django_internal_lock.set(True)
        try:
            return super().update_or_create(*args, **kwargs)
        finally:
            _django_internal_lock.reset(token)

    def select_for_update(self, *args, **kwargs):
        """Refuse, because Postgres silently declines to lock anything here.

        `FOR UPDATE` against a view with INSTEAD OF triggers is accepted and
        then does nothing: Postgres takes a table-level RowShareLock and marks
        no rows, so a second transaction can UPDATE the same row straight
        away. Measured — see tests/test_select_for_update.py. The read-modify-
        write this exists to protect is completely unprotected, with no error
        and no warning, which is the worst way to not have a lock."""
        if _django_internal_lock.get():
            return super().select_for_update(*args, **kwargs)
        raise OverlayConfigurationError(
            f"select_for_update() isn't supported on {self.model.__name__} — it's an overlay "
            "model, so the query targets a view, and Postgres accepts FOR UPDATE against a "
            "view with INSTEAD OF triggers without locking any rows. It would appear to work "
            "and protect nothing.\n\n"
            "For a read-modify-write, do it in one statement: "
            "`.update(field=F('field') + 1)` is atomic here, because an expression that reads "
            "its own row is routed around the view and applied to the base table directly. For "
            "a longer critical section, take an advisory lock on the row's id:\n\n"
            "    with transaction.atomic(), connection.cursor() as cursor:\n"
            "        cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [TABLE_KEY, row_id])\n"
            "        ...  # read, modify, write\n\n"
            "See docs/operations/LIMITATIONS.md."
        )

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False, update_conflicts=False, **kwargs):
        """Reject the conflict-handling kwargs instead of quietly doing the
        wrong thing.

        Django puts `ON CONFLICT` on the relation it inserts into — here, the
        view. A view has no unique index, so `update_conflicts` fails outright
        ("no unique or exclusion constraint matching the ON CONFLICT
        specification") while `ignore_conflicts` is accepted and then does
        nothing: the real insert happens one level down inside the INSTEAD OF
        trigger, with no conflict clause, so the duplicate raises anyway. The
        silent one is the dangerous one — it only misbehaves in exactly the
        case the caller thought they'd handled."""
        if ignore_conflicts or update_conflicts:
            kwarg = "ignore_conflicts" if ignore_conflicts else "update_conflicts"
            raise OverlayConfigurationError(
                f"bulk_create({kwarg}=True) isn't supported on {self.model.__name__} — it's an "
                "overlay model, so the insert targets a view and Postgres has no unique index "
                "there to detect a conflict against. Catch IntegrityError around a plain "
                "bulk_create(), or filter the batch against the view first."
            )
        # Neither flag is forwarded: the branch above returns unless both are
        # falsy, so passing them says something the reader has to re-derive,
        # and Django cannot tell False from the default anyway. update_fields
        # and unique_fields ride along in kwargs, where Django rejects them
        # without update_conflicts -- which is its job, not this method's.
        return super().bulk_create(objs, batch_size=batch_size, **kwargs)


OverlayManager = models.Manager.from_queryset(OverlayQuerySet)
