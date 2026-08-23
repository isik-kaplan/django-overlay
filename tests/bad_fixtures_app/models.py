from django.db import models

from django_overlay.sources import SourceTable
from django_overlay.models import OverlayMeta, OverlayModel
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


class BadUniqueness(OverlayModel):
    """Deliberately invalid. Every uniqueness rule here is one django_overlay
    can't honour, so DjangoOverlayConfig.ready() refuses to boot — see
    bad_fixtures_settings.py. Not managed=False: an overlay model's tables are
    never created under these settings anyway, since booting fails first."""

    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    desk = models.OneToOneField("testapp.Vendor", on_delete=models.DO_NOTHING, null=True, related_name="bad_desk")

    class Meta:
        unique_together = [("first_name", "last_name")]
        constraints = [models.UniqueConstraint(fields=["email"], name="bad_email_uniq")]

    class OverlayMeta(OverlayMeta):
        table_name = "baduniqueness"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_personsource")
