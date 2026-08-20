"""Benchmarks for django-overlay. Not shipped in the wheel.

Everything here is for people working on the library from a source checkout.
`pyproject.toml` restricts the wheel to the `django_overlay` package, so this
directory exists only in the repository; the `django-overlay benchmark` command
is a thin shim in the installed package that imports this lazily and explains
itself when it is missing.
"""
