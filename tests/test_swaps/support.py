"""Building a candidate table, and asserting what a preflight said about it.

Shared because a candidate is the same thing in every one of these files: a
table shaped exactly like the current source, filled or not, analysed or not.
The assert_* helpers are here for a different reason -- they encode what a
finding assertion has to cover to be worth writing, and having one copy is
what keeps that consistent as the files multiply.

a_populated_tenant() is the other kind of shared setup: an overlay somebody
has already been using, with an override, a tombstone and an inbound reference
in place. A swap over an untouched overlay exercises none of what a swap is
dangerous for.
"""

from django_overlay.sources import SourceTable
from tests.testapp.models import Person, PersonProfile
from tests.testapp_shared.models import PersonSource


def green(cursor, original: str, name: str, *, copy_rows: bool = True, analyzed: bool = True) -> SourceTable:
    """A candidate table shaped exactly like `original`. INCLUDING ALL so the
    index-parity checks have something real to compare, and ANALYZE so the row
    estimate is an estimate of something."""
    cursor.execute(f'DROP TABLE IF EXISTS public."{name}"')
    cursor.execute(f'CREATE TABLE public."{name}" (LIKE public."{original}" INCLUDING ALL)')
    if copy_rows:
        cursor.execute(f'INSERT INTO public."{name}" SELECT * FROM public."{original}"')
    if analyzed:
        analyze(cursor, name)
    return SourceTable(schema="public", table=name)


def analyze(cursor, name: str) -> None:
    """Every candidate is analysed before it is verified, because an unanalysed
    one is a finding in its own right (S015) and would mask the emptiness check
    behind it. Call it again after seeding a candidate directly."""
    cursor.execute(f'ANALYZE public."{name}"')


def point_at(monkeypatch, model, source: SourceTable) -> None:
    monkeypatch.setattr(model._overlay_meta, "get_source", staticmethod(lambda: source))


def codes(report) -> set:
    return {finding.code for finding in report.findings}


def error_codes(report) -> set:
    return {finding.code for finding in report.errors}


def finding(report, code):
    """The one finding carrying this code.

    Asserting a code is in the report leaves the two things the report actually
    promises unpinned: the level, which is what decides whether a swap is
    blocked or merely reported, and the sentence, which is the whole of what an
    operator gets. A check that silently downgraded an error to a warning would
    sail through a test that only looked for the code."""
    matches = [f for f in report.findings if f.code == code]
    assert len(matches) == 1, f"expected exactly one {code}, got {sorted(f.code for f in report.findings)}"
    return matches[0]


def assert_finding(report, code, level, *fragments):
    """The finding, its level, and the parts of its message that carry meaning
    rather than phrasing -- a count, a table name, the word that says which of
    two things went wrong."""
    found = finding(report, code)
    assert found.level == level, f"{code} came back {found.level}, expected {level}\n{report}"
    for fragment in fragments:
        assert fragment in found.message, f"{code} message is missing {fragment!r}:\n  {found.message}"
    return found


def assert_header(report, label, current, candidate):
    """The line an operator reads first: which model, from which table, to
    which one.

    Every path through the preflight builds its own SwapReport, and a header
    naming the wrong pair is worse than no header -- it is the confirmation
    somebody acts on before cutting a production source over. `(nothing
    deployed)` where a current source would go is itself the answer to a
    question, so it is asserted the same way.
    """
    assert str(report).splitlines()[0] == f"{label}: {current} -> {candidate}"


def assert_message(report, code, level, message):
    """The whole sentence, not a fragment of it.

    A finding *is* the output of a preflight -- an operator reads it and
    decides, on the strength of it alone, whether to cut a production source
    table over. A fragment assertion pins the clause it quotes and leaves every
    other clause free to say anything at all, including the opposite of what it
    says now, which is how a warning ends up describing the wrong failure.

    So one of these per finding code, alongside the fragment assertions that
    cover the scenarios. It makes rewording a finding a test change on purpose:
    here the wording is the feature, in a way it is not for an exception whose
    message nobody acts on.
    """
    found = finding(report, code)
    assert found.level == level, f"{code} came back {found.level}, expected {level}\n{report}"
    assert found.message == message, f"{code} says\n  {found.message}\nexpected\n  {message}"
    return found


class _Rollback(Exception):
    """Unwinds the atomic block below, and nothing else."""


def a_populated_tenant(db_cursor):
    """The four states a base table can be in relative to its source, all at
    once: untouched, overridden, tombstoned, and referenced."""
    untouched = PersonSource.objects.create(first_name="Ada", age=36)
    overridden = PersonSource.objects.create(first_name="Grace", age=45)
    deleted = PersonSource.objects.create(first_name="Alan", age=41)

    # Touching a source-backed row copies it down; the base copy shadows the
    # source row from then on.
    Person.objects.filter(pk=-overridden.id).update(first_name="Grace H.")
    # Soft delete leaves a tombstone that hides the source row from the view.
    Person.objects.filter(pk=-deleted.id).delete()
    PersonProfile.objects.create(person_id=-untouched.id, bio="referenced")
    # The source is a candidate too, the moment a swap is rolled back, and the
    # row-estimate check reads reltuples rather than counting. Leaving that
    # unanalysed makes the test depend on whatever ran before it having
    # analysed the table -- which held locally and did not on CI, where the
    # source reported zero rows and the roll-back was refused as empty.
    analyze(db_cursor, "testapp_shared_personsource")
    return untouched, overridden, deleted
