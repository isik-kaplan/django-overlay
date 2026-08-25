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
        constraints = [
            models.CheckConstraint(condition=models.Q(name__gt=""), name="metatest_name_not_empty"),
            OverlayUniqueConstraint(fields=["name"], name="metatest_name_unique"),
        ]
        indexes = [models.Index(fields=["name"], name="metatest_name_idx")]
        db_table_comment = "Meta forwarding test fixture"

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "metatest"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_metatestsource")


class MetaTestNote(models.Model):
    """Bonus FK table pointing at a source-less overlay model."""

    meta_test = OverlayForeignKey(MetaTest, on_delete=models.CASCADE, related_name="notes")
    text = models.TextField()

    class Meta:
        app_label = "testapp"


class NullableFkTest(models.Model):
    address = OverlayForeignKey(Address, on_delete=models.SET_NULL, null=True, related_name="nullable_fk_tests")

    class Meta:
        app_label = "testapp"


class UniqueTestNoSource(OverlayModel):
    """The model that opts *out* of soft delete, so the hard-delete path stays
    covered now that soft delete is the default.

    It used to be sourceless as well, and was named for that; that is
    now illegal, and the name was doing two jobs anyway. Deleting a row here
    removes it outright; where the row shadows a source row, the source row
    comes back, which is exactly what opting out of soft delete means."""

    ssn = models.CharField(max_length=20)

    class Meta:
        constraints = [OverlayUniqueConstraint(fields=["ssn"], name="uniquetestnosource_ssn_unique")]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "uniquetestnosource"
        soft_delete = False

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_uniquetestnosoftdeletesource")


class FilteredSourceTest(OverlayModel):
    first_name = models.CharField(max_length=100)

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "filteredsourcetest"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_filteredsourcetestsource", extra_where="active")


class UniqueTest(OverlayModel):
    ssn = models.CharField(max_length=20)
    notes = models.CharField(max_length=100, blank=True, default="")

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
            return SourceTable(schema="public", table="testapp_shared_renamefieldtestsource")


class RenameFkTest(models.Model):
    renamed_fk = OverlayForeignKey(Person, on_delete=models.CASCADE, related_name="rename_fk_tests")

    class Meta:
        app_label = "testapp"


class RemovableFkTest(models.Model):
    """`address` existed in migration 0010 and was removed in 0011 —
    exercises RemoveOverlayConstraint."""

    label = models.CharField(max_length=100, default="x")

    class Meta:
        app_label = "testapp"


class RemovableUniqueTest(OverlayModel):
    """The ssn OverlayUniqueConstraint existed in migration 0012 and was
    removed in 0013 — exercises RemoveOverlayUniqueConstraint."""

    ssn = models.CharField(max_length=20)

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "removableuniquetest"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_removableuniquetestsource")


# Mutable on purpose: stands in for however a real app resolves the
# current tenant's vendor (settings, a config table, connection.schema_name...).
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


class SoftDeleteTest(OverlayModel):
    first_name = models.CharField(max_length=100)

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "softdeletetest"
        soft_delete = True

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_softdeletetestsource")


class SoftDeleteTestNote(models.Model):
    target = OverlayForeignKey(SoftDeleteTest, on_delete=models.CASCADE, related_name="notes")
    text = models.TextField()

    class Meta:
        app_label = "testapp"


class DigitLeadingTableNameTest(OverlayModel):
    label = models.CharField(max_length=100, default="x")

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "123digitleadingtablenametest"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_digitleadingtablenametestsource")


class Vendor(models.Model):
    """Plain table an OverlayModel points at with an explicit related_name."""

    name = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp"


class VendorThing(OverlayModel):
    """OverlayModel declaring a plain FK with an explicit related_name — the
    shape that used to fail fields.E304 because the hidden base model
    declared the same reverse accessor."""

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="things")
    label = models.CharField(max_length=100, default="x")

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "vendorthing"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_vendorthingsource")


class PersonNote(OverlayModel):
    """OverlayModel pointing at another OverlayModel — the FK trigger lands
    on this model's base table and checks Person's base table plus source."""

    person = OverlayForeignKey(Person, on_delete=models.CASCADE, related_name="overlay_notes")
    text = models.TextField()

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "personnote"
        # Hard delete, so the non-overridable regression tests still exercise
        # the trigger's existence re-check: soft delete flags the referencing
        # row instead of removing it, and the stale-tuple problem that guard
        # exists for only arises when the row is genuinely gone.
        soft_delete = False

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_personnotesource")


class LabelManager(models.Manager):
    def labelled(self):
        return self.filter(label="labelled")


class CustomManagerTest(OverlayModel):
    """Declares its own manager, so django_overlay leaves it alone rather than
    replacing it with OverlayManager — which also means no bulk_create guard."""

    label = models.CharField(max_length=100, default="x")

    objects = LabelManager()

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "custommanagertest"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_custommanagertestsource")


class SoftDeleteUniqueTest(OverlayModel):
    """soft_delete plus all three ways of declaring uniqueness. Each has to
    end up as a partial index so a tombstone stops reserving its value."""

    ssn = models.CharField(max_length=20)
    email = models.CharField(max_length=100)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)

    class Meta:
        # unique_together and unique=True are rejected on a soft_delete model —
        # both come out as table constraints, which can't carry the
        # tombstone predicate. Meta.constraints is the supported spelling.
        constraints = [
            OverlayUniqueConstraint(fields=["ssn"], name="softdeleteuniquetest_ssn_unique"),
            OverlayUniqueConstraint(
                fields=["first_name", "last_name"], name="softdeleteuniquetest_first_name_last_name_uniq"
            ),
            OverlayUniqueConstraint(fields=["email"], name="softdeleteuniquetest_email_uniq"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "softdeleteuniquetest"
        soft_delete = True

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_softdeleteuniquetestsource")


class SoftDeletePlainUniqueTest(OverlayModel):
    """A CheckConstraint that must be left alone by the uniqueness narrowing,
    plus the ForeignKey-instead-of-OneToOneField shape the error message
    recommends. Sourceless once; neither of those jobs needed it
    to be."""

    code = models.CharField(max_length=20)
    tag = models.CharField(max_length=20, blank=True, default="")
    # A OneToOneField keeps working — the base model gets the plain ForeignKey
    # underneath, and the uniqueness comes from the constraint below.
    vendor = models.OneToOneField(Vendor, on_delete=models.CASCADE, null=True, related_name="plain_thing")

    class Meta:
        constraints = [
            OverlayUniqueConstraint(fields=["vendor"], name="softdeleteplainuniquetest_vendor_uniq"),
            OverlayUniqueConstraint(fields=["code"], name="softdeleteplainuniquetest_code"),
            models.CheckConstraint(condition=models.Q(code__gt=""), name="softdeleteplainuniquetest_code_not_empty"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "softdeleteplainuniquetest"
        soft_delete = True

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_softdeleteplainuniquetestsource")


class NullableUniqueTest(OverlayModel):
    """A sourced model with a nullable constrained column.

    Every other unique model here constrains a NOT NULL column, which left the
    `NEW.<col> IS NOT NULL` guard in unique_constraint_trigger.sql.j2 with no
    way to be exercised — SQL treats NULLs as non-colliding and the trigger has
    to agree, or one NULL badge in the source would block every NULL badge of
    your own."""

    badge = models.CharField(max_length=20, null=True)
    label = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        constraints = [OverlayUniqueConstraint(fields=["badge"], name="nullableuniquetest_badge_uniq")]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "nullableuniquetest"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_nullableuniquetestsource")


# ---------------------------------------------------------------------------
# Wide benchmark schema (tests/probe_wide_scale.py).
#
# Four overlay models and two plain ones, wired the way a real application
# wires them: an overlay model pointing at a plain table, a plain table
# pointing back at an overlay model, and overlay-to-overlay foreign keys two
# hops deep. Around ten columns each, five of them indexed on WideCustomer so
# indexed and unindexed access can be compared on the same table.
# ---------------------------------------------------------------------------


class WideRegion(models.Model):
    """A plain table an overlay model points at."""

    name = models.CharField(max_length=100)
    country = models.CharField(max_length=2)

    class Meta:
        app_label = "testapp"


class WideCustomer(OverlayModel):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.CharField(max_length=200)
    age = models.IntegerField(null=True)
    city = models.CharField(max_length=100)
    postcode = models.CharField(max_length=20)
    status = models.CharField(max_length=20)
    score = models.IntegerField(null=True)
    registered_on = models.DateField(null=True)
    notes = models.TextField(blank=True, default="")
    region = models.ForeignKey(WideRegion, on_delete=models.SET_NULL, null=True, related_name="customers")

    class Meta:
        # Indexed: last_name, city, status, score, age.
        # Unindexed on purpose: first_name, email, postcode, registered_on, notes.
        indexes = [
            models.Index(fields=["last_name"], name="widecustomer_last_name_idx"),
            models.Index(fields=["city"], name="widecustomer_city_idx"),
            models.Index(fields=["status"], name="widecustomer_status_idx"),
            models.Index(fields=["score"], name="widecustomer_score_idx"),
            models.Index(fields=["age"], name="widecustomer_age_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "widecustomer"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_widecustomersource")


class WideProduct(OverlayModel):
    sku = models.CharField(max_length=40)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=60)
    price_cents = models.IntegerField()
    weight_grams = models.IntegerField(null=True)
    supplier = models.CharField(max_length=100)
    discontinued = models.BooleanField(default=False)
    description = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["category"], name="wideproduct_category_idx"),
            models.Index(fields=["sku"], name="wideproduct_sku_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "wideproduct"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_wideproductsource")


class WideOrder(OverlayModel):
    """Overlay model with an overlay foreign key -- both ends are views."""

    reference = models.CharField(max_length=40)
    status = models.CharField(max_length=20)
    total_cents = models.IntegerField()
    placed_on = models.DateField(null=True)
    channel = models.CharField(max_length=30)
    currency = models.CharField(max_length=3)
    comment = models.TextField(blank=True, default="")
    customer = OverlayForeignKey(WideCustomer, on_delete=models.CASCADE, related_name="orders")

    class Meta:
        indexes = [
            models.Index(fields=["status"], name="wideorder_status_idx"),
            models.Index(fields=["channel"], name="wideorder_channel_idx"),
            models.Index(fields=["customer"], name="wideorder_customer_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "wideorder"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_wideordersource")


class WideOrderLine(OverlayModel):
    """Two overlay foreign keys, so a join from here to a customer crosses
    three views."""

    quantity = models.IntegerField()
    unit_price_cents = models.IntegerField()
    discount_cents = models.IntegerField(default=0)
    note = models.CharField(max_length=200, blank=True, default="")
    order = OverlayForeignKey(WideOrder, on_delete=models.CASCADE, related_name="lines")
    product = OverlayForeignKey(WideProduct, on_delete=models.CASCADE, related_name="lines")

    class Meta:
        indexes = [
            models.Index(fields=["order"], name="wideorderline_order_idx"),
            models.Index(fields=["product"], name="wideorderline_product_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "wideorderline"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_wideorderlinesource")


class WideCustomerNote(models.Model):
    """A plain table pointing back at an overlay model."""

    customer = OverlayForeignKey(WideCustomer, on_delete=models.CASCADE, related_name="customer_notes")
    body = models.TextField()
    author = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp"
        indexes = [models.Index(fields=["customer"], name="widecustomernote_customer_idx")]


class HardDeleteCountTest(OverlayModel):
    """soft_delete = False *with* a source — the one combination no other test
    model has, and the branch where count()'s base-side subquery carries no
    `WHERE NOT _overlay_deleted`."""

    first_name = models.CharField(max_length=100)

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "harddeletecounttest"
        soft_delete = False

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_harddeletecounttestsource")


class Member(OverlayModel):
    name = models.CharField(max_length=100)

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "member"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_membersource")


class Roster(OverlayModel):
    """Both ends of the M2M are overlay models and so is the through model, so
    `roster.members.all()` is a three-view traversal."""

    title = models.CharField(max_length=100)

    members = OverlayManyToManyField(Member, through="RosterMembership", related_name="rosters")

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "roster"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_rostersource")


class RosterMembership(OverlayModel):
    """An OverlayModel used as an M2M `through` — a vendor-asserted link the
    tenant can add to and remove from, but not edit in place.

    overridable = False with soft_delete on, so the view's anti-join narrows to
    tombstones rather than disappearing: removing a vendor-asserted membership
    has to keep working."""

    roster = OverlayForeignKey(Roster, on_delete=models.CASCADE)
    member = OverlayForeignKey(Member, on_delete=models.CASCADE)
    role = models.CharField(max_length=50, default="member")

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "rostermembership"
        overridable = False

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_rostermembershipsource")


class AuditEntry(OverlayModel):
    """overridable = False with hard delete — the case where nothing in the
    base table can ever collide with a source id, so the view carries no
    anti-join at all."""

    note = models.CharField(max_length=100)

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "auditentry"
        overridable = False
        soft_delete = False

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_auditentrysource")


class MemberUuid7Polyfill(OverlayModel):
    name = models.CharField(max_length=100)

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "member_uuid7polyfill"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_memberuuid7polyfillsource")


class RosterUuid7Polyfill(OverlayModel):
    title = models.CharField(max_length=100)

    members = OverlayManyToManyField(
        MemberUuid7Polyfill, through="RosterMembershipUuid7Polyfill", related_name="rosters"
    )

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "roster_uuid7polyfill"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_rosteruuid7polyfillsource")


class RosterMembershipUuid7Polyfill(OverlayModel):
    """The shape the normalised design actually ships: an overlay through table
    under a uuid strategy, non-overridable."""

    roster = OverlayForeignKey(RosterUuid7Polyfill, on_delete=models.CASCADE)
    member = OverlayForeignKey(MemberUuid7Polyfill, on_delete=models.CASCADE)
    role = models.CharField(max_length=50, default="member")

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "rostermembership_uuid7polyfill"
        overridable = False

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_rostermembershipuuid7polyfillsource")


# --------------------------------------------------------------------------
# UUID7 counterparts of the Wide* benchmark schema — see the note in
# testapp_shared. UUID7_POLYFILL rather than UUID7 because the development box
# is Postgres 17.6 and native uuidv7() needs 18.
# --------------------------------------------------------------------------


class WideCustomerU7(OverlayModel):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.CharField(max_length=200)
    age = models.IntegerField(null=True)
    city = models.CharField(max_length=100)
    postcode = models.CharField(max_length=20)
    status = models.CharField(max_length=20)
    score = models.IntegerField(null=True)
    registered_on = models.DateField(null=True)
    notes = models.TextField(blank=True, default="")
    region = models.ForeignKey(WideRegion, on_delete=models.SET_NULL, null=True, related_name="u7_customers")

    class Meta:
        indexes = [
            models.Index(fields=["last_name"], name="widecustomeru7_last_name_idx"),
            models.Index(fields=["city"], name="widecustomeru7_city_idx"),
            models.Index(fields=["status"], name="widecustomeru7_status_idx"),
            models.Index(fields=["score"], name="widecustomeru7_score_idx"),
            models.Index(fields=["age"], name="widecustomeru7_age_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "widecustomer_u7"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_widecustomeru7source")


class WideProductU7(OverlayModel):
    sku = models.CharField(max_length=40)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=60)
    price_cents = models.IntegerField()
    weight_grams = models.IntegerField(null=True)
    supplier = models.CharField(max_length=100)
    discontinued = models.BooleanField(default=False)
    description = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["category"], name="wideproductu7_category_idx"),
            models.Index(fields=["sku"], name="wideproductu7_sku_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "wideproduct_u7"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_wideproductu7source")


class WideOrderU7(OverlayModel):
    reference = models.CharField(max_length=40)
    status = models.CharField(max_length=20)
    total_cents = models.IntegerField()
    placed_on = models.DateField(null=True)
    channel = models.CharField(max_length=30)
    currency = models.CharField(max_length=3)
    comment = models.TextField(blank=True, default="")
    customer = OverlayForeignKey(WideCustomerU7, on_delete=models.CASCADE, related_name="orders")

    class Meta:
        indexes = [
            models.Index(fields=["status"], name="wideorderu7_status_idx"),
            models.Index(fields=["channel"], name="wideorderu7_channel_idx"),
            models.Index(fields=["customer"], name="wideorderu7_customer_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "wideorder_u7"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_wideorderu7source")


class WideOrderLineU7(OverlayModel):
    quantity = models.IntegerField()
    unit_price_cents = models.IntegerField()
    discount_cents = models.IntegerField(default=0)
    note = models.CharField(max_length=200, blank=True, default="")
    order = OverlayForeignKey(WideOrderU7, on_delete=models.CASCADE, related_name="lines")
    product = OverlayForeignKey(WideProductU7, on_delete=models.CASCADE, related_name="lines")

    class Meta:
        indexes = [
            models.Index(fields=["order"], name="wideorderlineu7_order_idx"),
            models.Index(fields=["product"], name="wideorderlineu7_product_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "wideorderline_u7"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_wideorderlineu7source")


class WideCustomerNoteU7(models.Model):
    customer = OverlayForeignKey(WideCustomerU7, on_delete=models.CASCADE, related_name="customer_notes")
    body = models.TextField()
    author = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp"
        indexes = [models.Index(fields=["customer"], name="widecustomernoteu7_customer_idx")]


class NullableFkOverlay(OverlayModel):
    """A nullable OverlayForeignKey between two overlay models.

    `WideOrder.customer` and friends are all NOT NULL, so without this there is
    no way to test that the traversal rewrite treats a NULL foreign key the way
    the join does. See tests/test_traversal_rewrite.py."""

    label = models.CharField(max_length=50)
    person = OverlayForeignKey(Person, on_delete=models.SET_NULL, null=True, related_name="nullable_overlay_refs")

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.NEGATIVE_ID)):
        table_name = "nullablefkoverlay"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_nullablefkoverlaysource")


# ---------------------------------------------------------------------------
# The production-shaped benchmark graph. Built by benchmark/graph.py; the
# models stay here because the permanent test suite depends on them too.
#
# Four entities the tenant may override and soft-delete, linked by three M2M
# through models it may only add to and remove from. The two halves sit at
# opposite ends of everything measured for ordered paging:
#
#   entities      overridable=True,  soft_delete=True   -> full anti-join, and
#                                                          a qual on the base
#                                                          branch. Both things
#                                                          that block an
#                                                          ordered path.
#   through       overridable=False, soft_delete=False  -> no anti-join and no
#                                                          qual. A bare
#                                                          UNION ALL, which is
#                                                          the O(limit) shape.
#
# So a person list is the slow shape, a membership traversal is the fast one,
# and any query crossing the two is the case worth measuring.
# ---------------------------------------------------------------------------


class BenchPerson(OverlayModel):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    city = models.CharField(max_length=100)
    postcode = models.CharField(max_length=20)
    status = models.CharField(max_length=20)
    score = models.IntegerField(null=True)
    born_on = models.DateField(null=True)
    notes = models.TextField(blank=True, default="")

    addresses = OverlayManyToManyField("BenchAddress", through="BenchPersonAddress", related_name="people")
    phones = OverlayManyToManyField("BenchPhone", through="BenchPersonPhone", related_name="people")
    emails = OverlayManyToManyField("BenchEmail", through="BenchPersonEmail", related_name="people")
    # Plain, not Overlay: BenchLabel is an ordinary table, so the auto-created
    # through table objection (checks.E002) does not apply and there is no view
    # on the far side to fence against. See BenchLabel.
    labels = models.ManyToManyField("BenchLabel", through="BenchPersonLabel", related_name="bench_people")

    class Meta:
        indexes = [
            models.Index(fields=["last_name"], name="bp_last_name_idx"),
            models.Index(fields=["city"], name="bp_city_idx"),
            models.Index(fields=["status"], name="bp_status_idx"),
            models.Index(fields=["score"], name="bp_score_idx"),
            models.Index(fields=["city", "-score"], name="bp_city_score_idx"),
            models.Index(fields=["last_name", "-score"], name="bp_lname_score_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "bench_person"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_benchpersonsource")


class BenchAddress(OverlayModel):
    line1 = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    postcode = models.CharField(max_length=20)
    country = models.CharField(max_length=2)

    class Meta:
        indexes = [
            models.Index(fields=["city"], name="ba_city_idx"),
            models.Index(fields=["postcode"], name="ba_postcode_idx"),
            models.Index(fields=["country"], name="ba_country_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "bench_address"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_benchaddresssource")


class BenchPhone(OverlayModel):
    number = models.CharField(max_length=32)
    kind = models.CharField(max_length=20)

    class Meta:
        indexes = [
            models.Index(fields=["number"], name="bh_number_idx"),
            models.Index(fields=["kind"], name="bh_kind_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "bench_phone"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_benchphonesource")


class BenchEmail(OverlayModel):
    address = models.CharField(max_length=200)
    domain = models.CharField(max_length=100)
    kind = models.CharField(max_length=20)

    class Meta:
        indexes = [
            models.Index(fields=["address"], name="be_address_idx"),
            models.Index(fields=["domain"], name="be_domain_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "bench_email"

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_benchemailsource")


class BenchPersonAddress(OverlayModel):
    person = OverlayForeignKey(BenchPerson, on_delete=models.CASCADE)
    address = OverlayForeignKey(BenchAddress, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, default="home")

    class Meta:
        indexes = [
            models.Index(fields=["person"], name="bpa_person_idx"),
            models.Index(fields=["address"], name="bpa_address_idx"),
            models.Index(fields=["person", "address"], name="bpa_pair_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "bench_person_address"
        overridable = False
        soft_delete = False

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_benchpersonaddresssource")


class BenchPersonPhone(OverlayModel):
    person = OverlayForeignKey(BenchPerson, on_delete=models.CASCADE)
    phone = OverlayForeignKey(BenchPhone, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, default="mobile")

    class Meta:
        indexes = [
            models.Index(fields=["person"], name="bpp_person_idx"),
            models.Index(fields=["phone"], name="bpp_phone_idx"),
            models.Index(fields=["person", "phone"], name="bpp_pair_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "bench_person_phone"
        overridable = False
        soft_delete = False

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_benchpersonphonesource")


class BenchPersonEmail(OverlayModel):
    person = OverlayForeignKey(BenchPerson, on_delete=models.CASCADE)
    email = OverlayForeignKey(BenchEmail, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, default="primary")

    class Meta:
        indexes = [
            models.Index(fields=["person"], name="bpe_person_idx"),
            models.Index(fields=["email"], name="bpe_email_idx"),
            models.Index(fields=["person", "email"], name="bpe_pair_idx"),
        ]

    class OverlayMeta(OverlayMeta.with_strategy(Strategy.UUID7_POLYFILL)):
        table_name = "bench_person_email"
        overridable = False
        soft_delete = False

        @staticmethod
        def get_source():
            return SourceTable(schema="public", table="testapp_shared_benchpersonemailsource")


# ---------------------------------------------------------------------------
# Plain mirrors of the benchmark graph: ordinary Django models, ordinary
# tables, ordinary ManyToManyField. No overlay machinery anywhere.
#
# These exist so the benchmark can compare ORM against ORM. Measuring an
# overlay queryset against hand-written SQL on a plain table charges the
# overlay for Django's own overhead and flatters the baseline; these carry the
# same fields, the same indexes and the same rows, so the only difference left
# is the view.
# ---------------------------------------------------------------------------


class PlainPerson(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    city = models.CharField(max_length=100)
    postcode = models.CharField(max_length=20)
    status = models.CharField(max_length=20)
    score = models.IntegerField(null=True)
    born_on = models.DateField(null=True)
    notes = models.TextField(blank=True, default="")

    addresses = models.ManyToManyField("PlainAddress", through="PlainPersonAddress", related_name="people")
    phones = models.ManyToManyField("PlainPhone", through="PlainPersonPhone", related_name="people")
    emails = models.ManyToManyField("PlainEmail", through="PlainPersonEmail", related_name="people")
    labels = models.ManyToManyField("BenchLabel", through="PlainPersonLabel", related_name="plain_people")

    class Meta:
        db_table = "plain_person"
        indexes = [
            models.Index(fields=["last_name"], name="pp_last_name_idx"),
            models.Index(fields=["city"], name="pp_city_idx"),
            models.Index(fields=["status"], name="pp_status_idx"),
            models.Index(fields=["score"], name="pp_score_idx"),
            models.Index(fields=["city", "-score"], name="pp_city_score_idx"),
            models.Index(fields=["last_name", "-score"], name="pp_lname_score_idx"),
        ]


class PlainAddress(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    line1 = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    postcode = models.CharField(max_length=20)
    country = models.CharField(max_length=2)

    class Meta:
        db_table = "plain_address"
        indexes = [
            models.Index(fields=["city"], name="pa_city_idx"),
            models.Index(fields=["postcode"], name="pa_postcode_idx"),
            models.Index(fields=["country"], name="pa_country_idx"),
        ]


class PlainPhone(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    number = models.CharField(max_length=32)
    kind = models.CharField(max_length=20)

    class Meta:
        db_table = "plain_phone"
        indexes = [
            models.Index(fields=["number"], name="ph_number_idx"),
            models.Index(fields=["kind"], name="ph_kind_idx"),
        ]


class PlainEmail(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    address = models.CharField(max_length=200)
    domain = models.CharField(max_length=100)
    kind = models.CharField(max_length=20)

    class Meta:
        db_table = "plain_email"
        indexes = [
            models.Index(fields=["address"], name="pe_address_idx"),
            models.Index(fields=["domain"], name="pe_domain_idx"),
        ]


class PlainPersonAddress(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    person = models.ForeignKey(PlainPerson, on_delete=models.CASCADE)
    address = models.ForeignKey(PlainAddress, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, default="home")

    class Meta:
        db_table = "plain_person_address"


class PlainPersonPhone(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    person = models.ForeignKey(PlainPerson, on_delete=models.CASCADE)
    phone = models.ForeignKey(PlainPhone, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, default="mobile")

    class Meta:
        db_table = "plain_person_phone"


class PlainPersonEmail(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    person = models.ForeignKey(PlainPerson, on_delete=models.CASCADE)
    email = models.ForeignKey(PlainEmail, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, default="primary")

    class Meta:
        db_table = "plain_person_email"


# ---------------------------------------------------------------------------
# The hybrid: a tenant-owned entity that is NOT behind a view.
#
# Every other relation in this graph joins two overlay views, and that is the
# shape that collapses -- probe_narrow_m2m_stall measured two m2m conditions
# estimating 267,425,037,000 rows for a 132-row answer, because neither side
# carries statistics.
#
# But not every entity has a vendor source to merge. A label, a saved list, a
# campaign is tenant-owned outright: there is no source row it could ever
# shadow, so there is no reason for it to be a view at all. Joining the person
# view to a plain table should be the good case -- the plain side has real
# statistics, and `_m2m_fence()` declines to fence it for exactly that reason
# (it requires both the through model and the target to be view models).
#
# BenchLabel is that entity, and it is deliberately an ordinary Model. The two
# link tables are ordinary too; only `BenchPersonLabel.person` needs an
# OverlayForeignKey, because its target is a view and Postgres cannot hold a
# real FK constraint against one.
# ---------------------------------------------------------------------------


class BenchLabel(models.Model):
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=20)

    class Meta:
        db_table = "bench_label"
        indexes = [
            models.Index(fields=["kind"], name="bl_kind_idx"),
            models.Index(fields=["name"], name="bl_name_idx"),
        ]


class BenchPersonLabel(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    person = OverlayForeignKey(BenchPerson, on_delete=models.CASCADE, related_name="label_links")
    label = models.ForeignKey(BenchLabel, on_delete=models.CASCADE, related_name="bench_links")

    class Meta:
        db_table = "bench_person_label"
        indexes = [
            models.Index(fields=["person"], name="bpl_person_idx"),
            models.Index(fields=["label"], name="bpl_label_idx"),
        ]


class PlainPersonLabel(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    person = models.ForeignKey(PlainPerson, on_delete=models.CASCADE, related_name="label_links")
    label = models.ForeignKey(BenchLabel, on_delete=models.CASCADE, related_name="plain_links")

    class Meta:
        db_table = "plain_person_label"
        indexes = [
            models.Index(fields=["person"], name="ppl_person_idx"),
            models.Index(fields=["label"], name="ppl_label_idx"),
        ]
