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
        indexes = [
            models.Index(fields=["ssn"], name="uts_ssn_idx"),
        ]


class UniqueTestCompositeSource(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["first_name", "last_name"], name="utcs_name_idx"),
        ]


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
        indexes = [
            models.Index(fields=["email"], name="sdus_email_idx"),
            models.Index(fields=["ssn"], name="sdus_ssn_idx"),
            models.Index(fields=["first_name", "last_name"], name="sdus_name_idx"),
        ]


class NullableUniqueTestSource(models.Model):
    """Source half of the nullable-unique pair. `badge` is nullable on both
    sides so the source-side uniqueness trigger's NULL guard is reachable."""

    badge = models.CharField(max_length=20, null=True)
    label = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["badge"], name="nuts_badge_idx"),
        ]


# ---------------------------------------------------------------------------
# Wide benchmark schema (tests/probe_wide_scale.py).
#
# Deliberately ordinary tables: ~10 columns, a mix of types, and no overlay
# machinery of their own -- these stand in for a vendor's data. The column
# lists have to match their overlay counterparts in testapp, because the view
# selects the overlay model's fields from both sides of the UNION.
# ---------------------------------------------------------------------------


class WideCustomerSource(models.Model):
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
    # The overlay model declares this as a ForeignKey; the vendor table just
    # carries the column. The type has to match the overlay model's column
    # exactly -- a wider type here makes the UNION ALL insert a cast, and a
    # cast on a join key rules out hash and merge joins entirely.
    region_id = models.IntegerField(null=True)

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["region_id"], name="wcs_region_id_idx"),
            models.Index(fields=["last_name"], name="wcs_last_name_idx"),
            models.Index(fields=["city"], name="wcs_city_idx"),
            models.Index(fields=["status"], name="wcs_status_idx"),
            models.Index(fields=["score"], name="wcs_score_idx"),
            models.Index(fields=["age"], name="wcs_age_idx"),
        ]


class WideProductSource(models.Model):
    sku = models.CharField(max_length=40)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=60)
    price_cents = models.IntegerField()
    weight_grams = models.IntegerField(null=True)
    supplier = models.CharField(max_length=100)
    discontinued = models.BooleanField(default=False)
    description = models.TextField(blank=True, default="")

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["category"], name="wps_category_idx"),
            models.Index(fields=["sku"], name="wps_sku_idx"),
        ]


class WideOrderSource(models.Model):
    reference = models.CharField(max_length=40)
    status = models.CharField(max_length=20)
    total_cents = models.IntegerField()
    placed_on = models.DateField(null=True)
    channel = models.CharField(max_length=30)
    currency = models.CharField(max_length=3)
    comment = models.TextField(blank=True, default="")
    # Points at a WideCustomer *view* id, so it can be negative. Must match
    # the overlay model's FK column type exactly; see WideCustomerSource.
    customer_id = models.IntegerField()

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["status"], name="wos_status_idx"),
            models.Index(fields=["channel"], name="wos_channel_idx"),
            models.Index(fields=["customer_id"], name="wos_customer_id_idx"),
        ]


class WideOrderLineSource(models.Model):
    quantity = models.IntegerField()
    unit_price_cents = models.IntegerField()
    discount_cents = models.IntegerField(default=0)
    note = models.CharField(max_length=200, blank=True, default="")
    order_id = models.IntegerField()
    product_id = models.IntegerField()

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["order_id"], name="wols_order_id_idx"),
            models.Index(fields=["product_id"], name="wols_product_id_idx"),
        ]


class HardDeleteCountTestSource(models.Model):
    first_name = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class RosterSource(models.Model):
    title = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class MemberSource(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class RosterMembershipSource(models.Model):
    """The vendor's assertion that a member belongs to a roster.

    IntegerField, not BigIntegerField. The overlay model declares these as
    OverlayForeignKeys against AutoField pks, so the columns are `integer`; a
    wider type here makes the UNION ALL insert a cast, and a cast on a join key
    rules out hash and merge joins entirely — this exact mismatch
    cost a measured 172x.
    """

    roster_id = models.IntegerField()
    member_id = models.IntegerField()
    role = models.CharField(max_length=50, default="member")

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["roster_id"], name="rms_roster_id_idx"),
            models.Index(fields=["member_id"], name="rms_member_id_idx"),
        ]


class AuditEntrySource(models.Model):
    note = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class RosterUuid7PolyfillSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class MemberUuid7PolyfillSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class RosterMembershipUuid7PolyfillSource(models.Model):
    """The same vendor-asserted link as RosterMembershipSource, under a uuid
    strategy — where the FK columns hold the target's real ids rather than
    negated ones, because the view rewrites nothing."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    roster_id = models.UUIDField()
    member_id = models.UUIDField()
    role = models.CharField(max_length=50, default="member")

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["roster_id"], name="rms7_roster_id_idx"),
            models.Index(fields=["member_id"], name="rms7_member_id_idx"),
        ]


# --------------------------------------------------------------------------
# UUID7 counterparts of the Wide* benchmark schema. Same columns, same widths,
# same nullability -- only the id and the overlay-pointing foreign keys change
# type, so the two strategies can be measured against identical data shapes.
# --------------------------------------------------------------------------


class WideCustomerU7Source(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
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
    # WideRegion stays a plain integer-keyed table, so this column does not
    # become a uuid just because the overlay model's pk did.
    region_id = models.IntegerField(null=True)

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["region_id"], name="wcs7_region_id_idx"),
            models.Index(fields=["last_name"], name="wcs7_last_name_idx"),
            models.Index(fields=["city"], name="wcs7_city_idx"),
            models.Index(fields=["status"], name="wcs7_status_idx"),
            models.Index(fields=["score"], name="wcs7_score_idx"),
            models.Index(fields=["age"], name="wcs7_age_idx"),
        ]


class WideProductU7Source(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sku = models.CharField(max_length=40)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=60)
    price_cents = models.IntegerField()
    weight_grams = models.IntegerField(null=True)
    supplier = models.CharField(max_length=100)
    discontinued = models.BooleanField(default=False)
    description = models.TextField(blank=True, default="")

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["category"], name="wps7_category_idx"),
            models.Index(fields=["sku"], name="wps7_sku_idx"),
        ]


class WideOrderU7Source(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=40)
    status = models.CharField(max_length=20)
    total_cents = models.IntegerField()
    placed_on = models.DateField(null=True)
    channel = models.CharField(max_length=30)
    currency = models.CharField(max_length=3)
    comment = models.TextField(blank=True, default="")
    customer_id = models.UUIDField(null=True)

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["status"], name="wos7_status_idx"),
            models.Index(fields=["channel"], name="wos7_channel_idx"),
            models.Index(fields=["customer_id"], name="wos7_customer_id_idx"),
        ]


class WideOrderLineU7Source(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    quantity = models.IntegerField()
    unit_price_cents = models.IntegerField()
    discount_cents = models.IntegerField(default=0)
    note = models.CharField(max_length=200, blank=True, default="")
    order_id = models.UUIDField(null=True)
    product_id = models.UUIDField(null=True)

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["order_id"], name="wols7_order_id_idx"),
            models.Index(fields=["product_id"], name="wols7_product_id_idx"),
        ]


class NullableFkOverlaySource(models.Model):
    """Source for the one model with a *nullable* OverlayForeignKey on an
    overlay model — the shape where a join and a semi-join could differ on
    NULL, and so the shape the traversal rewrite has to be tested against."""

    label = models.CharField(max_length=50)
    person_id = models.IntegerField(null=True)

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["person_id"], name="nfos_person_id_idx"),
        ]


# ---------------------------------------------------------------------------
# The production-shaped benchmark graph: four entities the tenant may override
# and soft-delete, linked by three M2M through models it may only add to.
#
# Every index here mirrors one on the overlay side. That is not decoration --
# an unindexed source branch turns any filter that cannot terminate early into
# a sequential scan of the whole vendor table, and a `(scope, sort)` index
# present on one branch but not the other silently costs the `Merge Append`.
# ---------------------------------------------------------------------------


class BenchPersonSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    city = models.CharField(max_length=100)
    postcode = models.CharField(max_length=20)
    status = models.CharField(max_length=20)
    score = models.IntegerField(null=True)
    born_on = models.DateField(null=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["last_name"], name="bps_last_name_idx"),
            models.Index(fields=["city"], name="bps_city_idx"),
            models.Index(fields=["status"], name="bps_status_idx"),
            models.Index(fields=["score"], name="bps_score_idx"),
            models.Index(fields=["city", "-score"], name="bps_city_score_idx"),
            models.Index(fields=["last_name", "-score"], name="bps_lname_score_idx"),
        ]


class BenchAddressSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    line1 = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    postcode = models.CharField(max_length=20)
    country = models.CharField(max_length=2)

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["city"], name="bas_city_idx"),
            models.Index(fields=["postcode"], name="bas_postcode_idx"),
            models.Index(fields=["country"], name="bas_country_idx"),
        ]


class BenchPhoneSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.CharField(max_length=32)
    kind = models.CharField(max_length=20)

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["number"], name="bhs_number_idx"),
            models.Index(fields=["kind"], name="bhs_kind_idx"),
        ]


class BenchEmailSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    address = models.CharField(max_length=200)
    domain = models.CharField(max_length=100)
    kind = models.CharField(max_length=20)

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["address"], name="bes_address_idx"),
            models.Index(fields=["domain"], name="bes_domain_idx"),
        ]


class BenchPersonAddressSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    person_id = models.UUIDField()
    address_id = models.UUIDField()
    role = models.CharField(max_length=20)

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["person_id"], name="bpas_person_idx"),
            models.Index(fields=["address_id"], name="bpas_address_idx"),
            models.Index(fields=["person_id", "address_id"], name="bpas_pair_idx"),
        ]


class BenchPersonPhoneSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    person_id = models.UUIDField()
    phone_id = models.UUIDField()
    role = models.CharField(max_length=20)

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["person_id"], name="bpps_person_idx"),
            models.Index(fields=["phone_id"], name="bpps_phone_idx"),
            models.Index(fields=["person_id", "phone_id"], name="bpps_pair_idx"),
        ]


class BenchPersonEmailSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    person_id = models.UUIDField()
    email_id = models.UUIDField()
    role = models.CharField(max_length=20)

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["person_id"], name="bpes_person_idx"),
            models.Index(fields=["email_id"], name="bpes_email_idx"),
            models.Index(fields=["person_id", "email_id"], name="bpes_pair_idx"),
        ]


# Source tables for models that used to have none.
#
# Each of these models tests a mechanism (Meta forwarding, field renames, a
# digit-leading table name, a plain FK with related_name, an overlay FK, a
# custom manager) rather than sourcelessness. Now that an overlay model must
# have a source, they get the smallest source table that keeps the shape they
# were declared to test.


class MetaTestSource(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"
        indexes = [models.Index(fields=["name"], name="metatestsource_name_idx")]


class RenameFieldTestSource(models.Model):
    renamed_field = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp_shared"


class DigitLeadingTableNameTestSource(models.Model):
    label = models.CharField(max_length=100, default="x")

    class Meta:
        app_label = "testapp_shared"


class VendorThingSource(models.Model):
    # A plain column rather than a FK: this is a vendor's table, and it holds
    # whatever id the referenced row has, with no constraint of ours on it.
    vendor_id = models.IntegerField()
    label = models.CharField(max_length=100, default="x")

    class Meta:
        app_label = "testapp_shared"
        indexes = [models.Index(fields=["vendor_id"], name="vendorthingsource_vendor_idx")]


class PersonNoteSource(models.Model):
    # Holds *view* ids, negated, the same way any source-side FK column must —
    # holds ids the view exposes, not the vendor's own.
    person_id = models.IntegerField()
    text = models.TextField()

    class Meta:
        app_label = "testapp_shared"
        indexes = [models.Index(fields=["person_id"], name="personnotesource_person_idx")]


class CustomManagerTestSource(models.Model):
    label = models.CharField(max_length=100, default="x")

    class Meta:
        app_label = "testapp_shared"


class UniqueTestNoSoftDeleteSource(models.Model):
    ssn = models.CharField(max_length=20)

    class Meta:
        app_label = "testapp_shared"
        indexes = [models.Index(fields=["ssn"], name="utnsds_ssn_idx")]


class SoftDeletePlainUniqueTestSource(models.Model):
    code = models.CharField(max_length=20)
    tag = models.CharField(max_length=20, blank=True, default="")
    vendor_id = models.IntegerField(null=True)

    class Meta:
        app_label = "testapp_shared"
        indexes = [
            models.Index(fields=["code"], name="sdputs_code_idx"),
            models.Index(fields=["vendor_id"], name="sdputs_vendor_idx"),
        ]
