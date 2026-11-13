"""What an overlay update that changes nothing does. See RedundantUpdatePolicy.

Its own module, importing nothing of the package, so the SQL builders, the
view sync, the queryset and the model metaclass can all use it.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


REDUNDANT_UPDATE_MODES = ("materialize", "skip", "warn", "raise")
REDUNDANT_UPDATE_SCOPES = ("source_only", "all")


class RedundantUpdatePolicy:
    """What an update that changes nothing does, for one model.

    An update writes every column it is given, so `save()` after only reading
    a row is still an UPDATE, and on a row that exists only in the source it
    copies that row into the base table for good. Whether that is a problem,
    which columns don't count as a change (an `auto_now` timestamp always
    differs), and what to do about it are the project's to decide, so all of
    it is configuration:

    - `mode`: "materialize" does what any update does (the default); "skip"
      leaves the row as it is and reports the update as done; "warn" skips
      and says so (a Postgres WARNING from the trigger, a Python warning from
      the expression path); "raise" refuses, with OverlayRedundantUpdate.
    - `scope`: "source_only" applies the mode only where the update would copy
      a source row into the base table; "all" also to a row already there,
      where a redundant update is only a wasted write.
    - `ignored_columns`: columns left out of the comparison.

    Settings give the project's defaults (DJANGO_OVERLAY_REDUNDANT_UPDATES,
    DJANGO_OVERLAY_REDUNDANT_UPDATE_SCOPE, DJANGO_OVERLAY_REDUNDANT_UPDATE_IGNORE)
    and OverlayMeta's redundant_updates, redundant_update_scope and
    redundant_update_ignore override them per model. Ignored names are field
    names; a project-wide name a model lacks is skipped, a per-model one it
    lacks fails `manage.py check` (django_overlay.E008)."""

    def __init__(self, mode, scope, ignored_columns):
        self.mode = mode
        self.scope = scope
        self.ignored_columns = tuple(ignored_columns)

    @property
    def guarded(self) -> bool:
        return self.mode != "materialize"

    def __eq__(self, other):
        return isinstance(other, RedundantUpdatePolicy) and (self.mode, self.scope, self.ignored_columns) == (
            other.mode,
            other.scope,
            other.ignored_columns,
        )

    def __repr__(self):
        return f"RedundantUpdatePolicy({self.mode!r}, {self.scope!r}, {self.ignored_columns!r})"


def _choice(value, choices, where):
    if value not in choices:
        raise ImproperlyConfigured(f"{where} must be one of {', '.join(map(repr, choices))}, got {value!r}.")
    return value


def _names(value, where):
    if isinstance(value, str) or not all(isinstance(name, str) for name in value):
        raise ImproperlyConfigured(f"{where} must be a list or tuple of field names, got {value!r}.")
    return tuple(value)


def redundant_update_policy(model) -> RedundantUpdatePolicy:
    """`model`'s RedundantUpdatePolicy: its OverlayMeta where set, the project's
    settings otherwise. Read when a trigger is built or an update runs, not
    when the model is declared, so settings changed later still apply."""
    meta = model._overlay_meta
    mode = meta.redundant_updates
    if mode is None:
        mode = _choice(
            getattr(settings, "DJANGO_OVERLAY_REDUNDANT_UPDATES", "materialize"),
            REDUNDANT_UPDATE_MODES,
            "settings.DJANGO_OVERLAY_REDUNDANT_UPDATES",
        )
    scope = meta.redundant_update_scope
    if scope is None:
        scope = _choice(
            getattr(settings, "DJANGO_OVERLAY_REDUNDANT_UPDATE_SCOPE", "source_only"),
            REDUNDANT_UPDATE_SCOPES,
            "settings.DJANGO_OVERLAY_REDUNDANT_UPDATE_SCOPE",
        )
    if meta.redundant_update_ignore is None:
        names = _names(
            getattr(settings, "DJANGO_OVERLAY_REDUNDANT_UPDATE_IGNORE", ()),
            "settings.DJANGO_OVERLAY_REDUNDANT_UPDATE_IGNORE",
        )
    else:
        names = tuple(meta.redundant_update_ignore)  # checked when the model was declared
    fields = {field.name: field.column for field in model._meta.concrete_fields}
    # A name the model lacks is skipped here; per model, E008 reports it.
    return RedundantUpdatePolicy(mode, scope, [fields[n] for n in names if n in fields])
