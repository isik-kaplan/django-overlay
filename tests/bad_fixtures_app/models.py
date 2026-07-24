from django.db import models

from tests.testapp.models import Address, Person


class BadNote(models.Model):
    """Deliberately invalid, managed=False. Kept in its own app since
    DjangoOverlayConfig.ready() refuses to boot with a model like this
    installed — see bad_fixtures_settings.py."""

    person = models.ForeignKey(Person, on_delete=models.DO_NOTHING, related_name="bad_notes")

    class Meta:
        managed = False


class BadManyToMany(models.Model):
    """Deliberately invalid, managed=False. See BadNote."""

    addresses = models.ManyToManyField(Address, related_name="bad_many_to_many")

    class Meta:
        managed = False
