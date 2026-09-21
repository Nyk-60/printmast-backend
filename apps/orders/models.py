from django.db import models

from apps.core.models import TimeStampedModel


class Order(TimeStampedModel):
    class Status(models.TextChoices):
        NEW = 'new', 'New'
        MATCHING = 'matching', 'Awaiting Match'
        READY = 'ready', 'Ready for Production'
        SENT_TO_PRINT = 'sent_to_print', 'Sent to Print'
        IN_PRODUCTION = 'in_production', 'In Production'
        SHIPPED = 'shipped', 'Shipped'
        DELIVERED = 'delivered', 'Delivered'
        CANCELLED = 'cancelled', 'Cancelled'
        REFUNDED = 'refunded', 'Refunded'

    sub_user = models.ForeignKey(
        'accounts.SubUserProfile', related_name='orders', on_delete=models.PROTECT
    )
    store = models.ForeignKey(
        'shipping.Store', related_name='orders', on_delete=models.PROTECT
    )

    # ShipStation identifiers
    shipstation_order_id = models.CharField(max_length=64, db_index=True)
    shipstation_order_number = models.CharField(max_length=64, blank=True)
    external_order_number = models.CharField(max_length=64, blank=True, db_index=True)

    # Customer
    customer_name = models.CharField(max_length=200)
    customer_email = models.EmailField(blank=True)

    # Ship-to address
    ship_to_name = models.CharField(max_length=200)
    ship_to_company = models.CharField(max_length=200, blank=True)
    ship_to_address1 = models.CharField(max_length=200)
    ship_to_address2 = models.CharField(max_length=200, blank=True)
    ship_to_city = models.CharField(max_length=120)
    ship_to_state = models.CharField(max_length=80, blank=True)
    ship_to_postal_code = models.CharField(max_length=20)
    ship_to_country = models.CharField(max_length=2, default='US')
    address_verified = models.BooleanField(default=False)  # True only when address_status == validated

    class AddressStatus(models.TextChoices):
        UNKNOWN = 'unknown', 'Not validated'
        VALIDATED = 'validated', 'Validated'
        WARNING = 'warning', 'Warning'
        FAILED = 'failed', 'Failed'

    # Mirrors ShipStation's green/yellow/red address indicator (shipTo.addressVerified).
    # Only the status is exposed by the API — not the reason (e.g. "Apt Number Not Found").
    # A warning does not stop ShipStation from buying a label, so it must not block us either.
    address_status = models.CharField(
        max_length=10, choices=AddressStatus.choices, default=AddressStatus.UNKNOWN,
    )

    # Dates
    ordered_at = models.DateTimeField(null=True, blank=True)
    ship_by_date = models.DateField(null=True, blank=True)

    # Shipping
    requested_service = models.CharField(max_length=200, blank=True)  # raw from marketplace
    selected_carrier_code = models.CharField(max_length=80, blank=True)   # e.g. "stamps_com"
    selected_service_code = models.CharField(max_length=80, blank=True)   # e.g. "usps_ground_advantage"
    shipping_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    shipping_cost_raw = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)  # before markup

    # Label (filled after purchase)
    label_url = models.URLField(blank=True)
    tracking_number = models.CharField(max_length=120, blank=True)
    shipstation_shipment_id = models.CharField(max_length=64, blank=True)
    label_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    labeled_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Shipping override (PC-only: overrides auto-calculated weight/dims before ShipStation push)
    shipping_override = models.JSONField(
        null=True, blank=True,
        help_text='PC-set override: {weight_oz, length, width, height, note}. Null = use auto-calc.',
    )

    # Notes
    buyer_note    = models.TextField(blank=True)
    gift_message  = models.TextField(blank=True)
    is_gift       = models.BooleanField(default=False)
    internal_note = models.TextField(blank=True)
    print_note    = models.TextField(blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.NEW, db_index=True
    )
    is_hidden = models.BooleanField(
        default=False, db_index=True,
        help_text='Removed from platform view by the sub-user. Not cancelled in ShipStation.',
    )

    class Meta:
        ordering = ('-ordered_at', '-created_at')
        unique_together = ('store', 'shipstation_order_id')

    def __str__(self):
        return f'{self.shipstation_order_number or self.shipstation_order_id} — {self.customer_name}'


class OrderItem(TimeStampedModel):
    order = models.ForeignKey(Order, related_name='items', on_delete=models.CASCADE)

    # Raw data from ShipStation / marketplace
    shipstation_item_id = models.CharField(max_length=64, blank=True)
    marketplace_sku = models.CharField(max_length=200, blank=True)
    raw_product_name = models.CharField(max_length=300)
    raw_options = models.JSONField(default=dict, blank=True)  # {"Size": "XL", "Color": "Black"}
    personalization = models.TextField(blank=True)
    image_url = models.URLField(blank=True)
    custom_image = models.ImageField(upload_to='order_item_images/', blank=True, null=True)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Filled after product matching
    pc_variant = models.ForeignKey(
        'catalog.PrintCenterVariant',
        null=True, blank=True,
        related_name='order_items',
        on_delete=models.SET_NULL,
    )
    matched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('id',)

    def __str__(self):
        return f'{self.order} / {self.raw_product_name[:50]}'


class OrderEvent(TimeStampedModel):
    """Immutable audit trail entry for one order's lifecycle.

    Written explicitly at each meaningful transition (import, match, design
    upload, send to print, recall, label purchase, ...) rather than derived
    from field-change signals, so descriptions stay human-readable and the
    set of logged events stays deliberate instead of noisy.
    """

    class EventType(models.TextChoices):
        IMPORTED = 'imported', 'Imported'
        MATCHED = 'matched', 'Product Matched'
        DESIGN_UPLOADED = 'design_uploaded', 'Design Uploaded'
        DESIGN_REMOVED = 'design_removed', 'Design Removed'
        READY = 'ready', 'Ready for Print'
        SENT_TO_PRINT = 'sent_to_print', 'Sent to Print'
        RECALLED = 'recalled', 'Recalled to New Orders'
        LABEL_ATTEMPT = 'label_attempt', 'Label Purchase Attempted'
        LABEL_CONFIRMED = 'label_confirmed', 'Label Purchased'
        LABEL_FAILED = 'label_failed', 'Label Purchase Failed'
        IN_PRODUCTION = 'in_production', 'In Production'
        SHIPPED = 'shipped', 'Shipped'
        CANCELLED = 'cancelled', 'Cancelled'
        STATUS_SYNCED = 'status_synced', 'Status Synced from ShipStation'
        HIDDEN = 'hidden', 'Removed from Platform'
        SHIPSTATION_NOTIFIED = 'shipstation_notified', 'ShipStation Notified (Marketplace)'
        SHIPSTATION_NOTIFY_FAILED = 'shipstation_notify_failed', 'ShipStation Notify Failed'

    order = models.ForeignKey(Order, related_name='events', on_delete=models.CASCADE)
    event_type = models.CharField(max_length=30, choices=EventType.choices)
    actor = models.ForeignKey(
        'accounts.User', null=True, blank=True, on_delete=models.SET_NULL,
        help_text='Who triggered this — null for system/sync-driven events.',
    )
    description = models.CharField(max_length=300, blank=True)
    metadata = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ('created_at',)
        indexes = [models.Index(fields=['order', 'created_at'])]

    def __str__(self):
        return f'{self.order} — {self.get_event_type_display()}'


class ProductMapping(TimeStampedModel):
    """
    Remembered mapping: marketplace variant string → PrintCenterVariant.
    Scoped to a sub-user + optionally a store.
    """
    sub_user = models.ForeignKey(
        'accounts.SubUserProfile', related_name='product_mappings', on_delete=models.CASCADE
    )
    store = models.ForeignKey(
        'shipping.Store', null=True, blank=True,
        related_name='product_mappings', on_delete=models.SET_NULL
    )
    marketplace_sku = models.CharField(max_length=200, blank=True, db_index=True)
    marketplace_variant = models.CharField(max_length=300, db_index=True)
    pc_variant = models.ForeignKey(
        'catalog.PrintCenterVariant', on_delete=models.CASCADE, related_name='mappings'
    )

    class Meta:
        unique_together = ('sub_user', 'store', 'marketplace_sku', 'marketplace_variant')
        indexes = [
            models.Index(fields=['sub_user', 'marketplace_sku', 'marketplace_variant']),
        ]

    def __str__(self):
        return f'{self.marketplace_variant} → {self.pc_variant}'
