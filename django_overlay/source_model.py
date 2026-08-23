"""A read-only Django model over a model's source table.

The overlay gives you one ORM entry point per model, the view, and the view
cannot show you everything. Its anti-join removes any source row a base row
shadows, so the vendor's *original* values for a row you have overridden are
not reachable through it at all. Until now the only way to see them was
`reset_to_source()`, which destroys the override to get there.

So: `Person.source_table()` is a model over the vendor's table, and
`person.source_row()` is the row behind this one. Read-only, because writing
the vendor's table is the thing this library exists to prevent.

Three things this gets right that a hand-written model would not:

**It is invisible to the app registry.** The model is built against a private
`Apps()` instance, so Django never sees two models claiming one `db_table` —
which is exactly what would happen in a project that already declares its own
`PersonSource`, and would earn a `models.W035` for it. Nothing here appears in
migrations, `get_models()`, or system checks.

**`extra_where` is applied.** The view splices it into the source branch, so a
model that ignored it would return rows the overlay treats as nonexistent.
Same table, two different answers, and only one of them is the overlay's.

Built with `Field.clone()` rather than `copy.deepcopy()`, and that is not a
style preference. Django's `Field.__deepcopy__` is a shallow copy, so a copied
field shares the original's `cached_col` -- and if anything has already queried
the base model, that cache is a column qualified with the *base* table. The
generated SQL then selects `"person"."age"` from the vendor's table. It passes
in isolation and fails once something else has run first, which is the worst
way for a bug to behave. `clone()` rebuilds from `deconstruct()` and carries no
cached state.

**Ids are the vendor's, and the API keeps you from tripping over that.** Under
NEGATIVE_ID the view calls source row 5 `-5`. Rather than paper over it, the
mapping lives in `source_row()` / `view_pk_for()`, so the common question --
"what did the vendor have for *this* row?" -- never asks you to convert
anything yourself.
"""

from django.apps.registry import Apps
from django.db import models

from .strategies import negates_source_ids


SHADOW_FLAG = "_overlay_deleted"


# An auto field is a promise that the *database* generates the value, and a
# foreign key column is no such thing -- it holds whatever the referenced row's
# pk happens to be. Copying the target's pk verbatim gives a second auto field
# and Django refuses the model outright ("can't have more than one
# auto-generated field"), so the auto-ness is dropped and only the width kept.
_PLAIN_FOR_AUTO = {
    models.AutoField: models.IntegerField,
    models.BigAutoField: models.BigIntegerField,
    models.SmallAutoField: models.SmallIntegerField,
}


def _plain_copy(field):
    """`field` as something that can sit on a flat, relation-free model.

    A relation field would try to point at an overlay model and drag the whole
    view machinery in behind it. The vendor's table holds a bare column, so
    that is what this returns: the target's pk type, de-auto'd, under the
    foreign key's own column name.
    """
    if field.is_relation:
        target = field.target_field
        plain = _PLAIN_FOR_AUTO.get(type(target))
        copied = plain(null=field.null) if plain is not None else target.clone()
        copied.primary_key = False
        copied.remote_field = None
        copied.null = field.null
        copied.db_column = field.column
        return field.attname, copied
    copied = field.clone()
    copied.remote_field = None
    return field.name, copied


class ReadOnlySourceManager(models.Manager):
    """Scoped to what the overlay considers real.

    `extra_where` is raw SQL spliced into the view, so it is spliced here the
    same way. `.unfiltered()` is the way out, named rather than defaulted: a
    caller who gets the vendor's whole table by accident has no way to notice,
    and one who asks for it has said so.
    """

    def __init__(self, extra_where=""):
        super().__init__()
        self._extra_where = extra_where

    def get_queryset(self):
        queryset = super().get_queryset()
        if self._extra_where:
            queryset = queryset.extra(where=[self._extra_where])
        return queryset

    def unfiltered(self):
        return super().get_queryset()


def build_source_model(view_model):
    """The source model for `view_model`, or None if it has no source."""
    source = view_model.get_source()
    if source is None:
        return None

    base = view_model.base_table()
    attrs = {
        "__module__": view_model.__module__,
        "objects": ReadOnlySourceManager(source.extra_where),
    }
    for field in base._meta.concrete_fields:
        if field.name == SHADOW_FLAG:
            continue
        name, copied = _plain_copy(field)
        if field.primary_key:
            copied.db_column = source.id_column
        attrs[name] = copied

    def _refuse(self, *args, **kwargs):
        raise NotImplementedError(
            f"{view_model.__name__}'s source table is the vendor's and is read-only here. "
            f"Write through {view_model.__name__} instead — the overlay's whole job is to keep "
            "your edits in your own table."
        )

    attrs["save"] = _refuse
    attrs["delete"] = _refuse
    attrs["Meta"] = type(
        "Meta",
        (),
        {
            # A private registry: nothing below is a real installed model, so
            # two models never claim one db_table and W035 never fires.
            "apps": Apps(),
            "app_label": "django_overlay_source",
            "db_table": f'"{source.schema}"."{source.table}"',
            "managed": False,
        },
    )
    return type(f"{view_model.__name__}SourceRow", (models.Model,), attrs)


def view_pk_for(view_model, source_pk):
    """The pk the view gives a source row — negated, or not, per the strategy."""
    if negates_source_ids(view_model._overlay_meta.strategy):
        return -source_pk
    return source_pk


def source_pk_for(view_model, view_pk):
    """The inverse: which source row a view pk is talking about."""
    return view_pk_for(view_model, view_pk)
