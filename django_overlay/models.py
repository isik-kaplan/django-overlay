import contextvars
import copy

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections, models, transaction

from . import uniqueness
from .exceptions import OverlayConfigurationError
from .fields import base_model_copy, hide_reverse_side
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


class OverlayQuerySet(models.QuerySet):
    """Default queryset for every overlay view model."""

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
