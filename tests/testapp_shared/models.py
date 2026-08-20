import uuid

from django.db import models


class PersonSource(models.Model):
    first_name = models.CharField(max_length=100)
    age = models.IntegerField(null=True)

    class Meta:
        app_label = "testapp_shared"


class AddressSource(models.Model):
    street = models.CharField(max_length=100)
    city = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class PhoneSource(models.Model):
    number = models.CharField(max_length=20)

    class Meta:
        app_label = "testapp_shared"


class PersonSourceUuid4(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    first_name = models.CharField(max_length=100)
    age = models.IntegerField(null=True)

    class Meta:
        app_label = "testapp_shared"


class AddressSourceUuid4(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    street = models.CharField(max_length=100)
    city = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class PhoneSourceUuid4(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.CharField(max_length=20)

    class Meta:
        app_label = "testapp_shared"


class PersonSourceUuid7Polyfill(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    first_name = models.CharField(max_length=100)
    age = models.IntegerField(null=True)

    class Meta:
        app_label = "testapp_shared"


class AddressSourceUuid7Polyfill(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    street = models.CharField(max_length=100)
    city = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class PhoneSourceUuid7Polyfill(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.CharField(max_length=20)

    class Meta:
        app_label = "testapp_shared"


class ReservedWordSource(models.Model):
    order = models.IntegerField()

    class Meta:
        app_label = "testapp_shared"


class UniqueTestSource(models.Model):
    ssn = models.CharField(max_length=20)
    notes = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        app_label = "testapp_shared"


class UniqueTestCompositeSource(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class ProviderAPersonSource(models.Model):
    first_name = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class ProviderBPersonSource(models.Model):
    first_name = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class FilteredSourceTestSource(models.Model):
    first_name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)

    class Meta:
        app_label = "testapp_shared"


class RemovableUniqueTestSource(models.Model):
    ssn = models.CharField(max_length=20)

    class Meta:
        app_label = "testapp_shared"


class SoftDeleteTestSource(models.Model):
    first_name = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class SoftDeleteUniqueTestSource(models.Model):
    ssn = models.CharField(max_length=20)
    email = models.CharField(max_length=100)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"
