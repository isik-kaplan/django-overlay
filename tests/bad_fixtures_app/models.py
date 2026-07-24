from django.db import models

from tests.testapp.models import Address, Person


class BadNote(models.Model):
    """Deliberately invalid; managed=False so it never reaches the
    database. Kept in its own app, excluded from the main test settings'
    INSTALLED_APPS, because django_overlay.apps.DjangoOverlayConfig.ready()
    now refuses to boot while a model like this is installed — see
    tests/bad_fixtures_settings.py."""

    person = models.ForeignKey(Person, on_delete=models.DO_NOTHING, related_name="bad_notes")

    class Meta:
        managed = False


class BadManyToMany(models.Model):
    """Deliberately invalid; managed=False so it never reaches the
    database. See BadNote's docstring for why this lives in its own app."""

    addresses = models.ManyToManyField(Address, related_name="bad_many_to_many")

    class Meta:
        managed = False
