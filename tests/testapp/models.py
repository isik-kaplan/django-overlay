from django.db import models

from django_overlay.constraints import OverlayUniqueConstraint
from django_overlay.fields import OverlayForeignKey, OverlayManyToManyField, OverlayOneToOneField
from django_overlay.models import OverlayMeta, OverlayModel
from django_overlay.sources import SourceTable


Strategy = OverlayModel.Strategy


class Person(OverlayModel):
    first_name = models.CharField(max_length=100)
    age = models.IntegerField(null=True)

    addresses = OverlayManyToManyField("Address", through="PersonAddressThrough", related_name="people")
    phones = OverlayManyToManyField("Phone", through="PersonPhoneThrough", related_name="people")

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "person"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_personsource")


class Address(OverlayModel):
    street = models.CharField(max_length=100)
    city = models.CharField(max_length=100)

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "address"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_addresssource")


class Phone(OverlayModel):
    number = models.CharField(max_length=20)

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "phone"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_phonesource")


class PersonAddressThrough(models.Model):
    person = OverlayForeignKey(Person, on_delete=models.CASCADE)
    address = OverlayForeignKey(Address, on_delete=models.CASCADE)
    label = models.CharField(max_length=100, default="home")

    class Meta:
        app_label = "testapp"


class PersonPhoneThrough(models.Model):
    person = OverlayForeignKey(Person, on_delete=models.CASCADE)
    phone = OverlayForeignKey(Phone, on_delete=models.CASCADE)
    label = models.CharField(max_length=100, default="mobile")

    class Meta:
        app_label = "testapp"


class PersonProfile(models.Model):
    person = OverlayOneToOneField(Person, on_delete=models.CASCADE, related_name="profile")
    bio = models.TextField(blank=True, default="")

    class Meta:
        app_label = "testapp"


class AddressNote(models.Model):
    address = OverlayForeignKey(Address, on_delete=models.CASCADE, related_name="notes")
    text = models.TextField()

    class Meta:
        app_label = "testapp"


class PhoneTag(models.Model):
    name = models.CharField(max_length=100)
    phones = OverlayManyToManyField(Phone, through="PhoneTagPhoneThrough", related_name="tags")

    class Meta:
        app_label = "testapp"


class PhoneTagPhoneThrough(models.Model):
    phonetag = models.ForeignKey(PhoneTag, on_delete=models.CASCADE)
    phone = OverlayForeignKey(Phone, on_delete=models.CASCADE)

    class Meta:
        app_label = "testapp"


class PersonUuid4(OverlayModel):
    first_name = models.CharField(max_length=100)
    age = models.IntegerField(null=True)

    addresses = OverlayManyToManyField("AddressUuid4", through="PersonAddressThroughUuid4", related_name="people")
    phones = OverlayManyToManyField("PhoneUuid4", through="PersonPhoneThroughUuid4", related_name="people")

    class OverlayMeta(OverlayMeta):
        table_name = "person_uuid4"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_personsourceuuid4")


class AddressUuid4(OverlayModel):
    street = models.CharField(max_length=100)
    city = models.CharField(max_length=100)

    class OverlayMeta(OverlayMeta):
        table_name = "address_uuid4"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_addresssourceuuid4")


class PhoneUuid4(OverlayModel):
    number = models.CharField(max_length=20)

    class OverlayMeta(OverlayMeta):
        table_name = "phone_uuid4"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_phonesourceuuid4")


class PersonAddressThroughUuid4(models.Model):
    person = OverlayForeignKey(PersonUuid4, on_delete=models.CASCADE)
    address = OverlayForeignKey(AddressUuid4, on_delete=models.CASCADE)
    label = models.CharField(max_length=100, default="home")

    class Meta:
        app_label = "testapp"


class PersonPhoneThroughUuid4(models.Model):
    person = OverlayForeignKey(PersonUuid4, on_delete=models.CASCADE)
    phone = OverlayForeignKey(PhoneUuid4, on_delete=models.CASCADE)
    label = models.CharField(max_length=100, default="mobile")

    class Meta:
        app_label = "testapp"


class PersonProfileUuid4(models.Model):
    person = OverlayOneToOneField(PersonUuid4, on_delete=models.CASCADE, related_name="profile")
    bio = models.TextField(blank=True, default="")

    class Meta:
        app_label = "testapp"


class AddressNoteUuid4(models.Model):
    address = OverlayForeignKey(AddressUuid4, on_delete=models.CASCADE, related_name="notes")
    text = models.TextField()

    class Meta:
        app_label = "testapp"


class PhoneTagUuid4(models.Model):
    name = models.CharField(max_length=100)
    phones = OverlayManyToManyField(PhoneUuid4, through="PhoneTagPhoneThroughUuid4", related_name="tags")

    class Meta:
        app_label = "testapp"


class PhoneTagPhoneThroughUuid4(models.Model):
    phonetag = models.ForeignKey(PhoneTagUuid4, on_delete=models.CASCADE)
    phone = OverlayForeignKey(PhoneUuid4, on_delete=models.CASCADE)

    class Meta:
        app_label = "testapp"


class PersonUuid7Polyfill(OverlayModel):
    first_name = models.CharField(max_length=100)
    age = models.IntegerField(null=True)

    addresses = OverlayManyToManyField(
        "AddressUuid7Polyfill", through="PersonAddressThroughUuid7Polyfill", related_name="people"
    )
    phones = OverlayManyToManyField(
        "PhoneUuid7Polyfill", through="PersonPhoneThroughUuid7Polyfill", related_name="people"
    )

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "person_uuid7polyfill"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_personsourceuuid7polyfill")


class AddressUuid7Polyfill(OverlayModel):
    street = models.CharField(max_length=100)
    city = models.CharField(max_length=100)

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "address_uuid7polyfill"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_addresssourceuuid7polyfill")


class PhoneUuid7Polyfill(OverlayModel):
    number = models.CharField(max_length=20)

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "phone_uuid7polyfill"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_phonesourceuuid7polyfill")


class PersonAddressThroughUuid7Polyfill(models.Model):
    person = OverlayForeignKey(PersonUuid7Polyfill, on_delete=models.CASCADE)
    address = OverlayForeignKey(AddressUuid7Polyfill, on_delete=models.CASCADE)
    label = models.CharField(max_length=100, default="home")

    class Meta:
        app_label = "testapp"


class PersonPhoneThroughUuid7Polyfill(models.Model):
    person = OverlayForeignKey(PersonUuid7Polyfill, on_delete=models.CASCADE)
    phone = OverlayForeignKey(PhoneUuid7Polyfill, on_delete=models.CASCADE)
    label = models.CharField(max_length=100, default="mobile")

    class Meta:
        app_label = "testapp"


class PersonProfileUuid7Polyfill(models.Model):
    person = OverlayOneToOneField(PersonUuid7Polyfill, on_delete=models.CASCADE, related_name="profile")
    bio = models.TextField(blank=True, default="")

    class Meta:
        app_label = "testapp"


class AddressNoteUuid7Polyfill(models.Model):
    address = OverlayForeignKey(AddressUuid7Polyfill, on_delete=models.CASCADE, related_name="notes")
    text = models.TextField()

    class Meta:
        app_label = "testapp"


class PhoneTagUuid7Polyfill(models.Model):
    name = models.CharField(max_length=100)
    phones = OverlayManyToManyField(
        PhoneUuid7Polyfill, through="PhoneTagPhoneThroughUuid7Polyfill", related_name="tags"
    )

    class Meta:
        app_label = "testapp"


class PhoneTagPhoneThroughUuid7Polyfill(models.Model):
    phonetag = models.ForeignKey(PhoneTagUuid7Polyfill, on_delete=models.CASCADE)
    phone = OverlayForeignKey(PhoneUuid7Polyfill, on_delete=models.CASCADE)

    class Meta:
        app_label = "testapp"


class MetaTest(OverlayModel):
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["name"]
        verbose_name = "meta test"
        constraints = [models.CheckConstraint(condition=models.Q(name__gt=""), name="metatest_name_not_empty")]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "metatest"

        @staticmethod
        def get_source():
            return None


class UniqueTest(OverlayModel):
    ssn = models.CharField(max_length=20)

    class Meta:
        constraints = [OverlayUniqueConstraint(fields=["ssn"], name="uniquetest_ssn_unique")]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "uniquetest"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_uniquetestsource")


class UniqueTestComposite(OverlayModel):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)

    class Meta:
        constraints = [
            OverlayUniqueConstraint(fields=["first_name", "last_name"], name="uniquetestcomposite_name_unique")
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "uniquetestcomposite"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_uniquetestcompositesource")


class RenameFieldTest(OverlayModel):
    renamed_field = models.CharField(max_length=100)

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "renamefieldtest"

        @staticmethod
        def get_source():
            return None


class RenameFkTest(models.Model):
    renamed_fk = OverlayForeignKey(Person, on_delete=models.CASCADE, related_name="rename_fk_tests")

    class Meta:
        app_label = "testapp"


# Module-level, mutable on purpose: stands in for "however a real app
# resolves which vendor a tenant is currently provisioned against" (a
# settings lookup, a config table, connection.schema_name under
# django_tenants, ...) — get_source() itself doesn't care how.
CURRENT_PROVIDER = {"value": "provider_a"}

_PROVIDER_TABLES = {
    "provider_a": "testapp_shared_providerapersonsource",
    "provider_b": "testapp_shared_providerbpersonsource",
}


class SwitchableSourceTest(OverlayModel):
    first_name = models.CharField(max_length=100)

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "switchablesourcetest"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table=_PROVIDER_TABLES[CURRENT_PROVIDER["value"]])


class ReservedWord(OverlayModel):
    order = models.IntegerField()

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "reservedword"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_reservedwordsource")
