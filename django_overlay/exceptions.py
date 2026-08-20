class OverlayConfigurationError(Exception):
    """A model, field or constraint is declared in a way django_overlay can't
    honour. Lives in its own module so constraints.py can raise it without
    importing models/, which imports back the other way."""
