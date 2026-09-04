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
