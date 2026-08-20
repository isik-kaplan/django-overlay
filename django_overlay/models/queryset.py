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

from ..exceptions import OverlayConfigurationError
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
        # Neither flag is forwarded: the branch above returns unless both are
        # falsy, so passing them says something the reader has to re-derive,
        # and Django cannot tell False from the default anyway. update_fields
        # and unique_fields ride along in kwargs, where Django rejects them
        # without update_conflicts -- which is its job, not this method's.
        return super().bulk_create(objs, batch_size=batch_size, **kwargs)


OverlayManager = models.Manager.from_queryset(OverlayQuerySet)
