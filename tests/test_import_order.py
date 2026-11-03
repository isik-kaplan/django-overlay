"""Every public module can be the first one a project imports.

A circular import only shows when the cycle is entered from the right end, and
inside the test run the models are long imported, so each case gets its own
interpreter, set up with no apps installed: the test apps would import the
models first and hide the cycle. That is the shape of a project whose own
models module imports django_overlay.fields before django_overlay.models.
"""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    ["django_overlay.fields", "django_overlay.models", "django_overlay.sources", "django_overlay.constraints"],
)
def test_a_module_can_be_imported_before_any_other(module):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import django; from django.conf import settings; settings.configure(INSTALLED_APPS=[]); "
            f"django.setup(); import {module}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
