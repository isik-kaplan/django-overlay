"""The metaclass that turns one `class Person(OverlayModel)` into a hidden
base table and the view model the application imports.
"""

import copy

from django.db import models

from .. import uniqueness
from ..exceptions import OverlayConfigurationError
from ..fields import base_model_copy, hide_reverse_side
from ..source_model import build_source_model, source_pk_for
from ..strategies import Strategy, default_id_field
from .meta import OverlayMeta, _split_meta_options
from .queryset import OverlayManager


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
            raise OverlayConfigurationError(f"{name}.OverlayMeta must implement get_source() returning a SourceTable.")
        if overlay_meta.get_source() is None:
            # An overlay model with no source is a view over one table, three
            # INSTEAD OF triggers routing writes straight back to it, and a
            # tombstone column that can never be set -- soft delete is decided
            # per row, and a row with nothing to mask is hard deleted. The
            # uniqueness machinery degenerates too: the source-side check has
            # no source to check.
            raise OverlayConfigurationError(
                f"{name}.OverlayMeta.get_source() returns None, but an overlay model exists to layer "
                "your table over a source table — without one there is nothing to overlay, and the "
                "view and triggers only cost you. Use a plain models.Model; if other overlay models "
                "need to point at it, OverlayForeignKey works from a plain model too."
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

    @classmethod
    def source_table(cls):
        """A read-only model over the vendor's table.

        The view cannot show you a source row that a base row shadows -- the
        anti-join is what removes it -- so the vendor's original values for an
        overridden row are unreachable through the ORM. This is the way to read
        them without `reset_to_source()`, which destroys the override to get
        there. See django_overlay/source_model.py, and note the ids are the
        vendor's rather than the view's.

        Built once per model and cached on the class: it is a type, and
        rebuilding it per call would make two rows of the same table compare
        unequal.
        """
        if "_source_model" not in cls.__dict__:
            cls._source_model = build_source_model(cls)
        return cls._source_model

    def source_row(self):
        """The vendor's row behind this one, or None if the vendor has none.

        The whole point of the source model, and the spelling that never asks
        the caller to convert an id: this row already knows its own view pk, so
        the strategy's mapping is applied here rather than by hand.
        """
        model = type(self).source_table()
        return model.objects.filter(pk=source_pk_for(type(self), self.pk)).first()

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
