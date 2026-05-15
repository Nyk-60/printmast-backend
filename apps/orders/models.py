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
    address_verified = models.BooleanField(default=False)

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

    # Notes
    buyer_note = models.TextField(blank=True)
    print_note = models.TextField(blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.NEW, db_index=True
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
