import contextvars
import copy

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import connections, models, transaction
from django.db.models import sql
from django.db.models.constants import LOOKUP_SEP
from django.db.models.sql.where import AND, WhereNode

from . import uniqueness
from .exceptions import OverlayConfigurationError
from .fields import (
    OverlayFencedIn,
    OverlayForeignKey,
    OverlayManyToManyField,
    base_model_copy,
    hide_reverse_side,
)
from .strategies import Strategy, default_id_field, negates_source_ids


__all__ = ["OverlayConfigurationError", "OverlayMeta", "OverlayModel", "OverlayModelBase", "OverlayQuerySet"]


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


def _rewrite_traversals_enabled() -> bool:
    """settings.DJANGO_OVERLAY_REWRITE_TRAVERSALS turns OverlayQuery's rewrite
    off. On by default, because leaving it off means every application author
    has to remember a 987x cliff forever."""
    configured = getattr(settings, "DJANGO_OVERLAY_REWRITE_TRAVERSALS", True)
    if not isinstance(configured, bool):
        raise ImproperlyConfigured(f"settings.DJANGO_OVERLAY_REWRITE_TRAVERSALS must be a bool, got {configured!r}.")
    return configured


_fence_suppressed = contextvars.ContextVar("django_overlay_fence_suppressed", default=False)


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


class OverlayQuery(sql.Query):
    """Rewrites `filter(fk__field=…)` across two overlay models into
    `filter(fk_id__in=<subquery>)`.

    A `UNION ALL` view is an appendrel and an appendrel parent carries no
    statistics, so a join between two of them is estimated at 1/200 and `LIMIT`
    then turns that into a nested loop that never terminates early. Measured at
    900,000 view rows: the traversal is 6,199.5ms, the subquery form is 6.3ms.
    See tests/probe_join_fixes.py.

    There is no lookup to register for this — `customer__city` is resolved by
    `names_to_path()` well before lookups or transforms are consulted — so the
    hook is `build_filter()`, which sees the whole `('customer__city', 'x')`
    expression before any join is made.

    A join and a semi-join are only interchangeable when the join cannot
    multiply rows and cannot be negated into different NULL semantics, so the
    rewrite is gated hard. Everything it refuses is listed in
    `_traversal_rewrite()`, and tests/test_traversal_rewrite.py asserts the
    rewritten and unrewritten forms return identical rows for each of them.
    """

    def build_filter(self, filter_expr, *args, **kwargs):
        rewritten = self._traversal_rewrite(filter_expr, **kwargs)
        if rewritten is not None:
            return super().build_filter(rewritten, *args, **kwargs)

        fence = self._m2m_fence(filter_expr, **kwargs)
        clause, needed_inner = super().build_filter(filter_expr, *args, **kwargs)
        if fence is None:
            return clause, needed_inner

        # AND, never replace. See `_m2m_fence()` — the join stays exactly as
        # Django built it, so row multiplicity is untouched.
        fenced, fenced_inner = super().build_filter(fence, *args, **kwargs)
        combined = WhereNode([clause, fenced], connector=AND)
        return combined, list(needed_inner) + list(fenced_inner)

    def build_lookup(self, lookups, lhs, rhs):
        """Resolve the fence's private lookup name without registering it anywhere.

        `_m2m_fence()` returns `("pk__overlay_fenced_in", …)`, and a primary key
        is an ordinary `UUIDField` or `AutoField` that this package does not
        own — so the only way to make Django resolve that name through the
        normal path was `models.Field.register_lookup()`, which installs it on
        every field of every model in the importing project.

        Django only needs the name to resolve *here*: `names_to_path()` runs
        with `fail_on_missing=False`, so an unrecognised trailing name is
        handed to this method as a lookup rather than rejected as a bad field.
        Constructing the lookup directly therefore keeps the fence working and
        leaves every field in the project untouched — including, deliberately,
        overlay models' own primary keys. See `OverlayFencedIn` for why this is
        not offered as a public way to scope a query.
        """
        if lookups == [OverlayFencedIn.lookup_name]:
            return OverlayFencedIn(lhs, rhs)
        return super().build_lookup(lookups, lhs, rhs)

    def split_exclude(self, *args, **kwargs):
        """`exclude()` across a multi-valued relation.

        Django builds a fresh inner query here and then calls `trim_start()` on
        it, which assumes a particular join layout and indexes into the alias
        map by position. The inner query is an OverlayQuery too, and its filter
        is no longer negated, so the fence would fire inside it, add a subquery
        condition, and send `trim_start()` off the end of the list with an
        IndexError.

        Suppressing it costs nothing. The outer filter is negated, which the
        fence already refuses — see `_m2m_fence()` — so this only makes the
        inner query agree with the outer one.
        """
        token = _fence_suppressed.set(True)
        try:
            return super().split_exclude(*args, **kwargs)
        finally:
            _fence_suppressed.reset(token)

    def _m2m_fence(self, filter_expr, **kwargs):
        """An extra `(path, value)` to AND onto an m2m traversal, or None.

        `filter(phones__number=…)` joins two views through a third, and every
        one of them is an appendrel the planner has no statistics for. The
        forward-FK rewrite cannot be reused here: it *replaces* the join with a
        semi-join, and a semi-join does not multiply rows. A person with three
        matching phones must appear three times, and measuring it showed
        exactly that — the replacement returned 6 rows where the join returned
        10.

        So this adds a condition instead of replacing one:

            pk = ANY (ARRAY(SELECT person FROM through WHERE phone IN (…)))

        which is *implied by the join Django already emitted*. If a row
        satisfies the join then some through row links it to a matching target,
        so its pk is necessarily in that set. A conjunct implied by an existing
        conjunct cannot change which rows match or how many times each appears
        — it only hands the planner an InitPlan where it previously had a blind
        estimate.

        Measured on the production-shaped graph at 300,000 people: 306.6ms ->
        0.4ms for a selective term, 7,896.5ms -> 105.9ms for a broad one, with
        the row counts identical in both cases.
        """
        if kwargs.get("branch_negated") or kwargs.get("current_negated"):
            # Under negation the conjunct stops being redundant: NOT (A AND B)
            # is not NOT A. The whole argument above depends on the fence being
            # ANDed with the thing that implies it.
            return None
        if _fence_suppressed.get():
            return None
        if not isinstance(filter_expr, (tuple, list)) or len(filter_expr) != 2:
            return None
        path, value = filter_expr
        if not isinstance(path, str) or "__" not in path:
            return None
        if hasattr(value, "resolve_expression"):
            return None
        if not _rewrite_traversals_enabled():
            return None

        head, _, rest = path.partition("__")
        try:
            field = self.model._meta.get_field(head)
        except FieldDoesNotExist:
            return None

        ends = self._m2m_ends(field)
        if ends is None:
            return None
        through, target, to_here, to_there = ends

        if not getattr(through, "_is_overlay_view_model", False):
            return None
        if not getattr(target, "_is_overlay_view_model", False):
            return None
        if not self._is_fenceable_tail(target, rest):
            return None

        # OverlayQuery again, so the inner `phone__number=` hop rewrites into
        # its own fenced subquery and the whole thing nests.
        links = models.QuerySet(model=through, query=OverlayQuery(through))
        links = links.filter(**{f"{to_there}__{rest}": value})
        return ("pk__overlay_fenced_in", links.values(to_here))

    @staticmethod
    def _m2m_ends(field):
        """(through, other model, fk back to this model, fk to the other one).

        Both directions, because the two matter for different reasons. Forward
        is `filter(phones__kind=…)`, written by application code. Reverse is
        `filter(people__id=…)` — which nobody writes, but it is what a related
        manager and `prefetch_related()` emit, so it is the shape a detail page
        actually runs.
        """
        if isinstance(field, OverlayManyToManyField):
            declaring = field
            through = field.remote_field.through
            target = field.remote_field.model
            forward = True
        elif isinstance(field, models.ManyToManyRel) and isinstance(field.field, OverlayManyToManyField):
            declaring = field.field
            through = field.through
            target = declaring.model
            forward = False
        else:
            return None

        try:
            to_declaring = declaring.m2m_field_name()
            to_related = declaring.m2m_reverse_field_name()
        except Exception:  # noqa: BLE001 - a non-standard through model
            return None

        if forward:
            return through, target, to_declaring, to_related
        return through, target, to_related, to_declaring

    @staticmethod
    def _is_fenceable_tail(target, rest) -> bool:
        """Is `rest` something the through model can filter on?

        A field path is, including one ending at the far model's primary key —
        unlike a forward FK, `people__id` still joins through the link table,
        so there is a join here worth fencing.

        `in` and `exact` are, because they are what `prefetch_related()` emits
        (`filter(people__in=[…])`) and they still need that join.

        Anything else — `isnull`, a transform, an unknown lookup — is not.
        """
        if rest in {"in", "exact"}:
            return True
        head = rest.split(LOOKUP_SEP)[0]
        if head in {"pk"}:
            return True
        try:
            target._meta.get_field(head)
        except FieldDoesNotExist:
            return False
        return True

    def _traversal_rewrite(self, filter_expr, **kwargs):
        """The rewritten `(path, value)`, or None to leave it alone."""
        if kwargs.get("branch_negated") or kwargs.get("current_negated"):
            # exclude() / ~Q(). `NOT (col = ANY(ARRAY(…)))` and Django's
            # anti-join construction differ on a NULL foreign key, and being
            # subtly wrong here is worse than being slow.
            return None
        if not isinstance(filter_expr, (tuple, list)) or len(filter_expr) != 2:
            return None
        path, value = filter_expr
        if not isinstance(path, str) or "__" not in path:
            return None
        if hasattr(value, "resolve_expression"):
            # F(), OuterRef(), Subquery(): the expression would be resolved
            # against the inner model instead of this one.
            return None
        if not _rewrite_traversals_enabled():
            return None

        head, _, rest = path.partition("__")
        try:
            field = self.model._meta.get_field(head)
        except FieldDoesNotExist:
            return None
        # Forward relations only, which `isinstance` already guarantees: a
        # reverse or m2m path resolves to a ManyToOneRel/ManyToManyField rather
        # than to the OverlayForeignKey itself. That matters because a reverse
        # or m2m traversal multiplies rows and a semi-join does not, so swapping
        # one for the other would silently deduplicate.
        if not isinstance(field, OverlayForeignKey) or not field.concrete:
            return None

        target = field.remote_field.model
        if not getattr(target, "_is_overlay_view_model", False):
            # view -> plain measured 1.2-1.3x; the plain side's statistics
            # rescue the estimate and there is nothing to fence.
            return None
        if rest in {"pk", target._meta.pk.name, target._meta.pk.attname}:
            # Django trims this to a local column and never joins at all.
            return None
        try:
            target._meta.get_field(rest.split("__")[0])
        except FieldDoesNotExist:
            # `customer__isnull`, `customer__in`, `customer__exact`: a lookup
            # on the relation itself, not a traversal through it.
            return None

        # Built without a manager on purpose: a custom default manager may
        # filter rows, which would apply to the subquery and not to the join.
        # OverlayQuery again, so a multi-hop path rewrites all the way down.
        inner = models.QuerySet(model=target, query=OverlayQuery(target)).filter(**{rest: value})
        return (f"{field.attname}__in", inner.values("pk"))


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
                lookup
                for lookup in clone._prefetch_related_lookups
                if lookup not in self._overlay_redirected
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
                "Pass a chunk_size, or drop the select_related() and let the prefetch happen "
                "on the whole queryset.",
            )
        return super().iterator(chunk_size=chunk_size)

    def _values(self, *fields, **expressions):
        # select_related() is a no-op under values() in Django too, so dropping
        # the prefetch here is what "leave it alone" means -- not a change.
        clone = super()._values(*fields, **expressions)
        if self._overlay_redirected:
            clone._prefetch_related_lookups = tuple(
                lookup
                for lookup in clone._prefetch_related_lookups
                if lookup not in self._overlay_redirected
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
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            **kwargs,
        )


OverlayManager = models.Manager.from_queryset(OverlayQuerySet)


def _default_strategy() -> Strategy:
    """Lets a project set its own default strategy via
    settings.DJANGO_OVERLAY_DEFAULT_STRATEGY instead of Strategy.UUID4. A
    model can still override this with .with_strategy(...)."""
    configured = getattr(settings, "DJANGO_OVERLAY_DEFAULT_STRATEGY", Strategy.UUID4)
    if not isinstance(configured, Strategy):
        raise ImproperlyConfigured(
            "settings.DJANGO_OVERLAY_DEFAULT_STRATEGY must be a django_overlay.strategies.Strategy "
            f"member (e.g. Strategy.NEGATIVE_ID), got {configured!r}."
        )
    return configured


def _default_soft_delete() -> bool:
    """Soft delete is the default, because it is the one that makes `.delete()`
    behave the way Django users expect: the row stays gone.

    Without it, deleting a source-backed row only drops your local copy, so the
    row reappears showing the vendor's pristine values — correct for the
    architecture, surprising as a default. A model that genuinely wants that
    (or a purely organic model, where a tombstone masks nothing and costs an
    index entry forever) can set `soft_delete = False`, and a project can flip
    the default back with settings.DJANGO_OVERLAY_DEFAULT_SOFT_DELETE."""
    configured = getattr(settings, "DJANGO_OVERLAY_DEFAULT_SOFT_DELETE", True)
    if not isinstance(configured, bool):
        raise ImproperlyConfigured(f"settings.DJANGO_OVERLAY_DEFAULT_SOFT_DELETE must be a bool, got {configured!r}.")
    return configured


# Options that emit DDL go on the base (managed=True) model; everything
# else (ordering, verbose_name, ...) goes on the view model, since that's
# what's actually queried. `constraints` can't simply go on both — duplicate
# constraint names across two models is models.E032 — so the view model
# reaches them through OverlayModel.get_constraints() instead.
_BASE_ONLY_META_OPTIONS = ("constraints", "indexes", "unique_together", "index_together", "db_table_comment")

# Neither model is a sound home for these: the view model is unmanaged, so
# create_permissions() silently skips it; the base model isn't something
# app code should reference. Reject instead of picking a bad default.
_UNSUPPORTED_META_OPTIONS = ("permissions", "default_permissions")

# Both models must agree on which app they belong to — Django can usually
# infer this from the module either model is defined in, but an explicit
# override needs to reach both, not just whichever side it happened to land on.
_BOTH_META_OPTIONS = ("app_label",)

# The metaclass sets these itself on both models — declaring your own would
# just get silently overwritten, so reject instead.
_FORCED_META_OPTIONS = {
    "db_table": "table naming is controlled entirely by OverlayMeta.table_name (defaults to the lowercased class name)",
    "managed": "the base model is always managed=True and the view model is always managed=False",
}


def _split_meta_options(model_name: str, user_meta) -> tuple[dict, dict]:
    if user_meta is None:
        return {}, {}
    options = {k: v for k, v in vars(user_meta).items() if not k.startswith("_")}
    forced = [k for k in _FORCED_META_OPTIONS if k in options]
    if forced:
        raise OverlayConfigurationError(
            f"{model_name}.Meta.{forced[0]} isn't supported on an OverlayModel — "
            f"{_FORCED_META_OPTIONS[forced[0]]}; it would just be silently overwritten."
        )
    unsupported = [k for k in _UNSUPPORTED_META_OPTIONS if k in options]
    if unsupported:
        raise OverlayConfigurationError(
            f"{model_name}.Meta.{unsupported[0]} isn't supported on an OverlayModel — there's no "
            "model to attach it to that makes sense (see _UNSUPPORTED_META_OPTIONS)."
        )
    base_options = {k: v for k, v in options.items() if k in _BASE_ONLY_META_OPTIONS + _BOTH_META_OPTIONS}
    view_options = {k: v for k, v in options.items() if k not in _BASE_ONLY_META_OPTIONS}
    return base_options, view_options


class OverlayMeta:
    """Base class for a model's inner OverlayMeta. Subclass it (directly,
    or via with_strategy()) and add table_name / get_source().

    `overridable = False` says a source row can never be edited in place —
    the tenant may add their own rows and (with soft_delete) hide vendor ones,
    but copy-on-write is refused. The clearest case is a many-to-many `through`
    model: a link row is a pair of ids, so there is nothing in it to edit.

    Declaring that buys a much cheaper view. The `NOT EXISTS` anti-join exists
    to stop a materialised row appearing twice; with nothing ever materialised
    it either disappears (hard delete) or narrows to tombstones only (soft
    delete), and unfiltered ordering goes from an `Append` over a
    `Hash Anti Join` to a `Merge Append` that `LIMIT` can stop early.

    It is enforced rather than assumed: the INSTEAD OF UPDATE trigger raises
    instead of copying the row down, so raw SQL cannot break the invariant the
    view now depends on either.

    Changing it does not generate a migration — like get_source(), it is
    OverlayMeta rather than Django model state, so nothing in the field list
    changes for makemigrations to notice. Run `manage.py resync_overlay_views`
    afterwards.

    No get_source() stub here on purpose: the metaclass requires every concrete
    overlay model to define one in its own OverlayMeta, so a NotImplementedError
    fallback could never run — and it read as reachable while being invisible to
    coverage, which excludes `raise NotImplementedError`."""

    Strategy = Strategy
    strategy = _default_strategy()
    soft_delete = _default_soft_delete()
    overridable = True
    pk_default_sql = None

    @classmethod
    def with_strategy(cls, strategy: Strategy):
        return type(f"OverlayMeta_{strategy.value}", (cls,), {"strategy": strategy})


def _base_field_copy(field):
    """The base model's copy of a declared field — see fields.base_model_copy
    and fields.hide_reverse_side for what differs from the view model's.

    Both models declare every concrete field, so a relation with an explicit
    related_name would be declared twice against the same target — a
    fields.E304/E305 clash that fails `manage.py check` at boot (and makes an
    OverlayForeignKey between two overlay models impossible). Hiding the base
    side also keeps Django's delete collector out of the hidden table: left
    visible, a cascade from the far end would delete base rows directly and
    walk straight past the view's INSTEAD OF triggers, so a soft_delete model
    would be hard-deleted."""
    copied = base_model_copy(field)
    if copied.remote_field is not None:
        hide_reverse_side(copied)
    return copied


class OverlayModelBase(models.base.ModelBase):
    """Splits one `class Person(OverlayModel)` into a hidden managed=True
    base table and the managed=False view model the app actually imports."""

    def __new__(mcs, name, bases, namespace, **kwargs):
        if namespace.pop("_overlay_root", False):
            return super().__new__(mcs, name, bases, namespace, **kwargs)

        is_overlay_subclass = any(isinstance(b, OverlayModelBase) for b in bases)
        meta = namespace.get("Meta")
        if not is_overlay_subclass or (meta is not None and getattr(meta, "abstract", False)):
            return super().__new__(mcs, name, bases, namespace, **kwargs)

        inherited = [base for base in bases if getattr(base, "_is_overlay_view_model", False)]
        if inherited:
            # Multi-table inheritance from a concrete overlay model. Django
            # would give the child a parent link to the *view*, which is
            # unmanaged and has no table of its own to point at. Caught here
            # because the next check would otherwise report a missing
            # OverlayMeta — true, but it sends you off writing one for a model
            # that can't work either way.
            raise OverlayConfigurationError(
                f"{name} subclasses {inherited[0].__name__}, which is an overlay model. Multi-table "
                "inheritance isn't supported: the parent link would point at a view rather than a "
                "table. Declare a separate OverlayModel, or put the shared fields on an abstract "
                "base (Meta.abstract = True) that both inherit."
            )

        overlay_meta = namespace.pop("OverlayMeta", None)
        if overlay_meta is None or not issubclass(overlay_meta, OverlayMeta):
            raise OverlayConfigurationError(f"{name}.OverlayMeta must subclass django_overlay.models.OverlayMeta.")
        if "get_source" not in overlay_meta.__dict__:
            raise OverlayConfigurationError(
                f"{name}.OverlayMeta must implement get_source() returning a SourceTable | None."
            )
        if not isinstance(overlay_meta.strategy, Strategy):
            raise OverlayConfigurationError(
                f"{name}.OverlayMeta.strategy must be a Strategy member (e.g. Strategy.NEGATIVE_ID "
                f"or .with_strategy(...)), got {overlay_meta.strategy!r}."
            )
        if not isinstance(overlay_meta.soft_delete, bool):
            raise OverlayConfigurationError(
                f"{name}.OverlayMeta.soft_delete must be a bool, got {overlay_meta.soft_delete!r}."
            )
        if not isinstance(overlay_meta.overridable, bool):
            raise OverlayConfigurationError(
                f"{name}.OverlayMeta.overridable must be a bool, got {overlay_meta.overridable!r}."
            )

        # M2M fields go on the view model only — copying one to both models
        # would create two independent through tables.
        m2m_items = {k: v for k, v in namespace.items() if isinstance(v, models.ManyToManyField)}
        field_items = {k: v for k, v in namespace.items() if isinstance(v, models.Field) and k not in m2m_items}
        rest_items = {k: v for k, v in namespace.items() if k not in field_items and k not in m2m_items}
        table_name = getattr(overlay_meta, "table_name", name.lower())

        if overlay_meta.soft_delete and "_overlay_deleted" in field_items:
            raise OverlayConfigurationError(
                f"{name} can't declare its own `_overlay_deleted` field — django_overlay reserves "
                "that name for its soft_delete shadow flag."
            )

        if "id" not in field_items:
            injected = default_id_field(overlay_meta.strategy)
            if injected is not None:
                field_items["id"] = injected

        base_meta_options, view_meta_options = _split_meta_options(name, namespace.get("Meta"))

        base_fields = {k: _base_field_copy(v) for k, v in field_items.items()}
        base_ns = {**rest_items, **base_fields}
        if overlay_meta.soft_delete:
            # Base-only shadow flag — never copied to the view model, so it
            # never shows up as a queryable column there.
            base_ns["_overlay_deleted"] = models.BooleanField(default=False, editable=False)
            # Every uniqueness rule has to ignore tombstoned rows, or a
            # soft-deleted row keeps its value reserved forever.
            base_meta_options = uniqueness.narrow_for_soft_delete(base_meta_options)
        base_ns["__qualname__"] = f"{name}Base"
        # No default_permissions for the base table — nobody should see
        # "Can add <name>base" in an admin permission list.
        base_ns["Meta"] = type("Meta", (), {**base_meta_options, "db_table": table_name, "default_permissions": ()})
        base_model = super().__new__(mcs, f"{name}Base", bases, base_ns, **kwargs)

        view_ns = {**rest_items, **{k: copy.deepcopy(v) for k, v in field_items.items()}, **m2m_items}
        wants_overlay_base_manager = False
        if not any(isinstance(v, models.Manager) for v in rest_items.values()):
            # Only when the model declares no manager of its own — overriding
            # someone's custom manager would be worse than missing the guard.
            view_ns["objects"] = OverlayManager()
            wants_overlay_base_manager = True
        view_ns["Meta"] = type("Meta", (), {**view_meta_options, "db_table": f"{table_name}_view", "managed": False})
        view_model = super().__new__(mcs, name, bases, view_ns, **kwargs)

        if wants_overlay_base_manager and "base_manager_name" not in view_meta_options:
            # instance.save() goes through _base_manager, which Django otherwise
            # builds as a plain Manager — so without this the routing in
            # OverlayQuerySet is reachable from update() but not from save().
            # Safe as a base manager: the overrides refuse or reroute writes and
            # filter nothing out.
            #
            # Set on _meta rather than in Meta above on purpose. Meta options go
            # into original_attrs, which the autodetector compares, so declaring
            # it there makes every project using this library owe an
            # AlterModelOptions migration for a manager the library chose. This
            # sets the same attribute without claiming the user declared it.
            view_model._meta.base_manager_name = "objects"

        view_model._base_model = base_model
        view_model._overlay_meta = overlay_meta
        view_model._is_overlay_view_model = True
        base_model._view_model = view_model
        return view_model


class OverlayModel(models.Model, metaclass=OverlayModelBase):
    _overlay_root = True

    Strategy = Strategy

    class Meta:
        abstract = True

    @classmethod
    def base_table(cls):
        """The hidden concrete model backing this view. Migration/tooling
        use only — application code should never query or write to it."""
        return cls._base_model

    @classmethod
    def get_source(cls):
        return cls._overlay_meta.get_source()

    def get_constraints(self):
        """Meta.constraints live on the hidden base model, because that's the
        model that emits their DDL — so Django's own implementation finds
        nothing to validate here and full_clean() would silently pass a value
        the database is going to reject.

        Reported against *this* model on purpose: a constraint validates by
        querying the model it's handed, and querying the view is exactly right
        — it spans base ∪ source, so an OverlayUniqueConstraint catches a
        collision with an untouched source row as well as with a local one.

        A soft_delete model's constraints carry a predicate on
        `_overlay_deleted`, a base-only column the view model can't resolve, so
        they're handed over un-narrowed — see uniqueness.for_validation()."""
        return [(type(self), uniqueness.for_validation(self._base_model._meta.constraints))]

    def reset_to_source(self):
        """Discard this row's local materialization/soft-deletion and fall
        back to whatever the source shows for its id (nothing, if there's no
        source row). Not a delete — doesn't run Django's on_delete collector,
        since the identity itself isn't necessarily going away. See
        docs/concepts/DELETION.md."""
        self._base_model.objects.filter(pk=self.pk).delete()
