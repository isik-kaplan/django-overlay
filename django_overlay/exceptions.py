from django.db import ProgrammingError


class OverlayConfigurationError(Exception):
    """A model, field or constraint is declared in a way django_overlay can't
    honour. Lives in its own module so constraints.py can raise it without
    importing models/, which imports back the other way."""


class OverlaySwapRefused(Exception):
    """A source swap was asked for and the preflight said no. Carries the
    report, so a caller that catches it can print the findings rather than
    just the summary line."""

    def __init__(self, report):
        self.report = report
        super().__init__(str(report))


class OverlayRedundantUpdate(ProgrammingError):
    """An update that changes nothing, refused under redundant_updates = "raise".

    Every ORM write raises it: save(), update() and bulk_update() through the
    view's trigger, and the expression path in Python. Raw SQL through the view
    gets the trigger's own error, which carries SQLSTATE OV001, a code of
    django-overlay's that no other trigger raises; Django surfaces it as a
    DatabaseError. Both messages start with "overlay redundant update"."""
