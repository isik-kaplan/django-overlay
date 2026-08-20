"""`python -m benchmark`, for a checkout with nothing installed.

The same command as `django-overlay benchmark`; that one goes through the shim
in the installed package, this one skips it.
"""

from benchmark.cli import benchmark


if __name__ == "__main__":
    benchmark()
