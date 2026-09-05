"""What a preflight found, and how a caller accepts part of it.

No database in any of these: a report is a list of findings and a verdict, so
these pin the two things a caller reads -- the sentence an operator acts on,
and what `allow=` does to a finding rather than for it.
"""

import pytest

from django_overlay.sources import SourceTable
from django_overlay.swaps import (
    ERROR,
    WARNING,
    Finding,
    SwapReport,
    _allow,
    verify_source_swap,
)
from tests.test_swaps.support import (
    codes,
    finding,
    green,
)
from tests.testapp.models import (
    UniqueTest,
)
from tests.testapp_shared.models import UniqueTestSource


pytestmark = pytest.mark.django_db


def test_a_report_with_no_findings_says_so():
    source = SourceTable(schema="public", table="whatever")
    assert "no findings" in str(SwapReport("testapp.UniqueTest", source, source, ()))


def test_warnings_are_separable_from_errors(db_cursor):
    UniqueTestSource.objects.create(ssn="111-11-1111")
    candidate = green(db_cursor, "testapp_shared_uniquetestsource", "green_uniquetest")

    report = verify_source_swap(UniqueTest, candidate)

    assert {f.code for f in report.warnings} == codes(report)
    assert not report.errors


def test_allowing_one_code_leaves_every_other_finding_alone():
    """`allow` names one code. Downgrading anything else would turn a list of
    accepted findings into a way of turning the preflight off, which is the one
    thing an escape hatch must not become."""
    source = SourceTable(schema="public", table="whatever")
    report = SwapReport(
        "testapp.UniqueTest",
        source,
        source,
        (
            Finding("S009", ERROR, "a collision"),
            Finding("S007", ERROR, "a dangling reference"),
            Finding("S006", WARNING, "an orphan"),
        ),
    )

    allowed = _allow(report, ["S009"])

    assert finding(allowed, "S009").level == WARNING
    assert "[allowed]" in finding(allowed, "S009").message
    # The other error is untouched, so the report still blocks.
    assert finding(allowed, "S007").level == ERROR
    assert not allowed.ok
    # And a warning whose code happens to be allowed is not re-marked: it was
    # never blocking, so there is nothing to accept.
    assert "[allowed]" not in finding(allowed, "S006").message
