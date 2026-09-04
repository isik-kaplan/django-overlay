import copy

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models.fields.related_lookups import RelatedIn
from django.db.models.lookups import In

from .models import OverlayConfigurationError
from .strategies import negates_source_ids


class OverlayForeignKey(models.ForeignKey):
    """FK for pointing at an OverlayModel. Postgres can't hold a real FK
    against a view, so this never creates a db constraint; referential
    integrity instead comes from a constraint trigger (see
    operations.AddOverlayConstraint)."""

    def __init__(self, to, *args, partition_column: str | None = None, **kwargs):
        if "db_constraint" in kwargs:
            raise OverlayConfigurationError(
                "OverlayForeignKey always sets db_constraint=False (Postgres can't hold a real FK "
                "against a view) — don't pass db_constraint yourself."
            )
        kwargs["db_constraint"] = False
        # Which column on *this* model carries the target source's partition
        # key. Only the insert-side trigger needs it, and only it needs telling
        # — see constraint_trigger.sql.j2 for why the other probes find the key
        # themselves. Omitted, the probe fans out across every partition: still
        # correct, just paying for a scan of all of them on every write.
        # `manage.py check` says so rather than leaving it to be discovered.
        self.partition_column = partition_column
        super().__init__(to, *args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs.pop("db_constraint", None)
        if self.partition_column is not None:
            # Serialized, because the trigger body is written from migration
            # state: a field that lost this on the round trip would rebuild
            # the unpruned probe the next time the migration was replayed.
            kwargs["partition_column"] = self.partition_column
        return name, path, args, kwargs

    def trigger_name(self, model) -> str:
        return f"overlayfk_{model._meta.db_table}_{self.column}"[:63]

    def referenced_row_trigger_name(self, model) -> str:
        """The delete-side guard. Lives on the target's base table, so the name
        has to identify the *referencing* side to stay unique among the
        triggers of a target with several things pointing at it."""
        return f"overlayfkdel_{model._meta.db_table}_{self.column}"[:63]

    def target_tables(self, tenant_schema: str) -> list[dict]:
        """The target's base table plus its source, if any — see target_tables_for()."""
        return target_tables_for(self.remote_field.model, tenant_schema, partition_column=self.partition_column)


def target_tables_for(
    target, tenant_schema: str, soft_delete: bool | None = None, partition_column: str | None = None
) -> list[dict]:
    """[{"schema", "table", "id_column", "negate", "soft_delete"}, ...] for
    `target`'s base table (never negated) and its source (negated for a
    NEGATIVE_ID target). Always both: an overlay model
    without a source is refused at declaration time. Takes the target model directly, not `self`, so a
    migration operation can call it against a *live* model even when the
    referencing field only exists in migration-historical state.

    `soft_delete` can be overridden for the same reason the column list is
    taken from historical state: a trigger rebuilt while replaying an older
    migration must not reference `_overlay_deleted` before the migration that
    adds it has run.

    `partition_column` is the referencing model's column holding the target
    source's partition key. It lands on the source entry only — the base table
    is an ordinary unpartitioned table, so a key predicate there would filter
    correctly and prune nothing. Both halves have to be present for the entry
    to appear: a source with no declared key has nothing to prune on, and a
    key with no local column has no value to prune by."""
    masks = target._overlay_meta.soft_delete if soft_delete is None else soft_delete
    base = {
        "schema": tenant_schema,
        "table": target._base_model._meta.db_table,
        "id_column": "id",
        "negate": False,
        "soft_delete": masks,
    }
    source = target.get_source()
    return [
        base,
        {
            "schema": source.schema,
            "table": source.table,
            "id_column": source.id_column,
            "negate": negates_source_ids(target._overlay_meta.strategy),
            "soft_delete": False,
            "partition": (
                {"column": source.partition_key, "local_column": partition_column}
                if source.partition_key and partition_column
                else None
            ),
            # A tombstone hides the source row from the view, so it has to hide
            # it from the FK check too. Without this the source branch accepts
            # a row the view will not return, and you get a reference you can
            # write and cannot read back. The
            # base branch already excludes tombstones; this is the same
            # exclusion, applied from the other side.
            #
            # `base` rather than a copy: the tombstone that masks a source row
            # lives in that exact table, under the un-negated id.
            "masked_by": base if masks else None,
        },
    ]


def _array_subquery_in_enabled() -> bool:
    """Lets a project turn the rewrite below off with
    settings.DJANGO_OVERLAY_ARRAY_SUBQUERY_IN = False, in case its own
    subqueries are large enough that materialising them costs more than the
    bad plan does."""
    configured = getattr(settings, "DJANGO_OVERLAY_ARRAY_SUBQUERY_IN", True)
    if not isinstance(configured, bool):
        raise ImproperlyConfigured(f"settings.DJANGO_OVERLAY_ARRAY_SUBQUERY_IN must be a bool, got {configured!r}.")
    return configured


class OverlaySubqueryIn(RelatedIn):
    """`col IN (subquery)` becomes `col = ANY (ARRAY(subquery))`.

    Semantically identical — SQL defines `IN` as `= ANY` and the three-valued
    NULL logic is the same — but it plans completely differently, and on an
    overlay model that is the difference between a page and a timeout.

    A `UNION ALL` view is an appendrel, and an appendrel parent carries no
    statistics: `examine_simple_variable()` has arms for `RTE_RELATION` and for
    `RTE_SUBQUERY && !rte->inh`, and a pulled-up UNION ALL is neither, so the
    planner falls back to `DEFAULT_NUM_DISTINCT` and estimates the join at
    1/200. With a `LIMIT` on top, the tuple fraction collapses, `add_path()`
    selects on startup cost alone, and the cheapest-startup plan is a nested
    loop that then never terminates early.

    `IN (subquery)` stays a semi-join and is costed with that broken estimate.
    `ARRAY(subquery)` is an InitPlan: it is evaluated once, before the outer
    plan is costed, so the outer query sees a plain array instead of a
    relation whose size it cannot guess.

    Measured at 900,000 view rows, against a 1.6ms plain-table baseline:
    the traversal Django emits today is 6,868.9ms, `IN (subquery)` is 727.4ms,
    and this is **3.9ms**.

    Subclasses `RelatedIn`, not `In`: a ForeignKey's `IN` lookup is the one
    that converts model instances to their primary keys and handles
    multi-column targets, and registering a plain `In` over the top of it
    silently breaks `filter(fk__in=[instance, ...])`.

    Registered on OverlayForeignKey only, which is exactly the case where the
    outer relation is a view and the estimate is therefore blind. A literal
    list is left alone — there is no subquery to fence, and Django's `IN` is
    already right.

    The cost is that the subquery is materialised: roughly 16 bytes per row for
    a uuid key. Fine for a selective filter, not for one matching millions —
    set DJANGO_OVERLAY_ARRAY_SUBQUERY_IN = False to opt out."""

    def as_sql(self, compiler, connection):
        if self.rhs_is_direct_value() or not _array_subquery_in_enabled():
            return super().as_sql(compiler, connection)
        return _array_in_sql(self, compiler, connection)


def _array_in_sql(lookup, compiler, connection):
    """`lhs = ANY (ARRAY(subquery))` — see OverlaySubqueryIn for why.

    A function rather than a shared method, because the two lookups that need
    it have deliberately different parents: OverlaySubqueryIn extends RelatedIn
    for a foreign key's instance-to-pk conversion, and OverlayFencedIn extends
    In because a primary key needs none of that. A zero-arg `super()` resolves
    against the class that *defined* the method, so one class borrowing the
    other's `as_sql` gets a super() pointing outside its own MRO — which raised
    TypeError on the fallback branch, reachable whenever the fenced lookup was
    handed a literal rhs, or (before the flags were split) whenever
    DJANGO_OVERLAY_ARRAY_SUBQUERY_IN was off. Each class keeps its own
    three-line as_sql now, and only the part with no super() call is shared.
    """
    lhs_sql, lhs_params = lookup.process_lhs(compiler, connection)
    rhs_sql, rhs_params = lookup.process_rhs(compiler, connection)
    # process_rhs already parenthesises the subquery, so `ARRAY` + that is
    # `ARRAY(SELECT ...)`.
    return f"{lhs_sql} = ANY (ARRAY{rhs_sql})", list(lhs_params) + list(rhs_params)


OverlayForeignKey.register_lookup(OverlaySubqueryIn)


class OverlayFencedIn(In):
    """The same `= ANY (ARRAY(…))` rewrite, on a primary key.

    `OverlaySubqueryIn` is registered on `OverlayForeignKey`, so it covers
    `filter(fk__in=<subquery>)`. The M2M fence needs it on the *primary key* of
    the model being filtered — `person.id = ANY (ARRAY(…))` — and that field is
    an ordinary `UUIDField` or `AutoField`, which the package does not own.

    **Deliberately registered nowhere.** `OverlayQuery.build_lookup()`
    constructs it directly when it sees the private name, so it resolves inside
    an overlay query and nowhere else. Registering it on `models.Field` — the
    previous arrangement — put a lookup on every field of every model in any
    project that imported this package, overlay or not, to serve one internal
    call site.

    Two things reach it, then. `OverlayQuery._m2m_fence()` adds one
    automatically, being the only caller that has established the fence is
    redundant with a join already in the query. And you can name it yourself,
    for the case below that no automatic rule can decide.

    Not reachable from `pk__in`, and that is a measured decision rather than
    caution. The array form wins only while the subquery is small: on a
    twenty-aggregate summary at 1,000,000 view rows it was 2.0x faster than a
    plain `IN` at a 25,000-row scope and 2.3x *slower* at a 1,000,000-row one,
    crossing over somewhere between. The library cannot see the scope size when
    it compiles a lookup, and it will not go looking -- every optimisation here
    is decided by query shape alone, because SQL that depends on database state
    is SQL you cannot read off the code. Wiring this into `pk__in` would
    therefore have to double the cost of every broad-scope query or none of
    them, with no way to tell which it was doing.

    So the decision goes to whoever can make it. The lookup name is usable by
    hand on any overlay model:

        Roster.objects.filter(pk__overlay_fenced_in=Member.objects.values("pk"))
        # -> "roster_view"."id" = ANY (ARRAY(SELECT U0."id" FROM "member_view" U0))

    That is the supported way to fence a scope, and the whole of the interface:
    reach for it when you know your subquery is selective, leave `pk__in` alone
    when you know it is not or cannot tell. Swept at 1,000,000 people by
    benchmark/suites/fence.py, against the same subquery left as a plain `IN`:
    x15-17 at a 200-row scope, x2.5 at 5,000, x1.1 at 50,000, and x1.0 at
    500,000. So it stops paying rather than turning into a penalty on this
    shape -- the 2.3x above is a twenty-aggregate summary, which is a different
    query and has not been swept. `OverlayQuery.build_lookup()`
    resolves the name without registering it, so it costs nothing on any field
    of any model -- and a plain model raises FieldError, because the resolution
    lives on the overlay query rather than on the field.

    Measured on the production-shaped graph at 300,000 people: the M2M
    traversal Django emits is 306.6ms selective / 7,896.5ms broad; with this
    fence added it is 0.4ms / 105.9ms, returning identical rows.
    """

    lookup_name = "overlay_fenced_in"

    def as_sql(self, compiler, connection):
        # Written out rather than borrowed from OverlaySubqueryIn: see
        # _array_in_sql for what borrowing it did to `super()`.
        #
        # No setting is read here, unlike its foreign-key counterpart. Whether
        # this lookup exists at all is DJANGO_OVERLAY_M2M_FENCE's decision, made
        # in OverlayQuery._m2m_fence(), and once one has been built the array
        # form is the only form worth compiling it to. A literal rhs still
        # declines, because that is a shape rather than a preference.
        if self.rhs_is_direct_value():
            return super().as_sql(compiler, connection)
        return _array_in_sql(self, compiler, connection)


class OverlayOneToOneField(OverlayForeignKey, models.OneToOneField):
    pass


# A OneToOneField *is* a ForeignKey plus `unique=True` — that's the whole
# difference, in Django's source as well as conceptually. These pairs say which
# ForeignKey each OneToOneField collapses to. tests/test_field_internals.py
# pins the "plus unique=True and nothing else" assumption against Django.
_WITHOUT_IMPLICIT_UNIQUE = {
    models.OneToOneField: (models.ForeignKey, models.ManyToOneRel),
    OverlayOneToOneField: (OverlayForeignKey, models.ManyToOneRel),
}


def base_model_copy(field):
    """The base model's copy of a declared field.

    Almost always just a deep copy. The exception is a OneToOneField, which
    Django implements as "a ForeignKey that is also unique" — and it emits that
    uniqueness as a *table* constraint, which is the one shape django_overlay
    can't use: a table constraint covers the base table only (never the source)
    and can't carry the soft_delete predicate.

    So the base model stores the ForeignKey half and nothing else. The
    uniqueness comes from the OverlayUniqueConstraint that uniqueness.check()
    insists on, which does cover the source and can be made partial. The *view*
    model — the one application code holds — keeps the real OneToOneField, so
    `desk.occupant` is still singular and every O2O descriptor still works.

    Nothing observes the swap: the base model's relation is hidden
    (related_name="+"), so it installs no descriptor and appears in no reverse
    accessor either way.
    """
    copied = copy.deepcopy(field)
    collapsed = _WITHOUT_IMPLICIT_UNIQUE.get(type(field))
    if collapsed is not None:
        field_class, rel_class = collapsed
        # Retyped in place rather than rebuilt from deconstruct(): this runs
        # while the model class is still being created, and
        # ForeignKey.deconstruct() consults the app registry for swappable
        # models, which isn't loaded yet. Retyping is exact because
        # OneToOneField.__init__ adds no instance state beyond `unique`, and
        # OneToOneRel.__init__ none beyond `multiple`. Both are pinned by
        # tests/test_field_internals.py, which fails loudly — naming this
        # function — if a future Django adds to either.
        copied.__class__ = field_class
        copied.remote_field.__class__ = rel_class
        copied.remote_field.multiple = True  # OneToOneRel.__init__ sets this False
        copied._unique = False
        # Field.unique is a cached_property and deepcopy carries the cached
        # True over from the declared field, so clearing _unique isn't enough:
        # the schema editor would still emit UNIQUE.
        copied.__dict__.pop("unique", None)
    return copied


def hide_reverse_side(field) -> None:
    """Stop `field` claiming a reverse accessor on its target.

    Both models django_overlay builds declare every concrete field, so a
    relation with an explicit related_name would be declared twice against the
    same target — a fields.E304/E305 clash at boot. Hiding the base model's
    side also keeps Django's delete collector out of the hidden table: left
    visible, a cascade from the far end would delete base rows directly and
    walk straight past the view's INSTEAD OF triggers.

    Two attributes, because Django keeps two. `remote_field.related_name` is
    what the live model resolves accessors from; `_related_name` is what
    RelatedField.deconstruct() serializes, and leaving it alone would put a
    historical base model in migration state that re-claims the view model's
    accessor. tests/test_field_internals.py pins both.
    """
    field.remote_field.related_name = "+"
    field._related_name = "+"


class OverlayManyToManyField(models.ManyToManyField):
    """M2M field for relating to an OverlayModel. Requires an explicit
    through= model with OverlayForeignKey fields — Django's auto-created
    through table always uses a plain ForeignKey, which can never be safe
    against a view."""

    def __init__(self, to, *args, through, **kwargs):
        super().__init__(to, *args, through=through, **kwargs)
