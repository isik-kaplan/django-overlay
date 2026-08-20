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
