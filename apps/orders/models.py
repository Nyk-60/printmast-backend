from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


ORDER_STATUS = (
    ('new', 'New'),
    ('matched', 'Matched'),
    ('in_production', 'In Production'),
    ('shipped', 'Shipped'),
    ('delivered', 'Delivered'),
    ('cancelled', 'Cancelled'),
)

SHIPPING_SERVICE = (
    ('standard', 'Standard'),
    ('priority', 'Priority'),
    ('express', 'Express'),
)


class Store(TimeStampedModel):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='stores', on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    shipstation_store_id = models.CharField(max_length=64, db_index=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('owner', 'shipstation_store_id')

    def __str__(self):
        return f'{self.name} ({self.owner.email})'


class Order(TimeStampedModel):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='orders', on_delete=models.CASCADE)
    store = models.ForeignKey(Store, related_name='orders', on_delete=models.PROTECT)
    external_order_number = models.CharField(max_length=64, db_index=True, help_text='Etsy/marketplace order number')
    shipstation_order_id = models.CharField(max_length=64, blank=True, db_index=True)

    customer_name = models.CharField(max_length=200)
    customer_email = models.EmailField(blank=True)

    ship_to_name = models.CharField(max_length=200)
    ship_to_address1 = models.CharField(max_length=200)
    ship_to_address2 = models.CharField(max_length=200, blank=True)
    ship_to_city = models.CharField(max_length=120)
    ship_to_state = models.CharField(max_length=80, blank=True)
    ship_to_postal_code = models.CharField(max_length=20)
    ship_to_country = models.CharField(max_length=2, default='US')
    address_verified = models.BooleanField(default=False)

    ship_by_date = models.DateField(null=True, blank=True)
    ordered_at = models.DateTimeField(null=True, blank=True)

    requested_service = models.CharField(max_length=80, blank=True)
    selected_service = models.CharField(max_length=20, choices=SHIPPING_SERVICE, blank=True)
    shipping_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    buyer_note = models.TextField(blank=True)
    print_note = models.TextField(blank=True)

    status = models.CharField(max_length=20, choices=ORDER_STATUS, default='new', db_index=True)

    class Meta:
        ordering = ('-ordered_at', '-created_at')

    def __str__(self):
        return f'{self.external_order_number} — {self.customer_name}'


class OrderItem(TimeStampedModel):
    order = models.ForeignKey(Order, related_name='items', on_delete=models.CASCADE)
    raw_product_name = models.CharField(max_length=300, help_text='Raw name from Etsy/marketplace')
    raw_variant = models.CharField(max_length=300, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    personalization = models.TextField(blank=True)

    # Filled after matching:
    catalog_variant = models.ForeignKey(
        'catalog.ProductVariant', null=True, blank=True,
        related_name='order_items', on_delete=models.SET_NULL
    )
    matched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('id',)


class UserProductMapping(TimeStampedModel):
    """Remembered mapping from a marketplace variant name to a catalog variant."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='product_mappings', on_delete=models.CASCADE)
    etsy_variant_name = models.CharField(max_length=300)
    catalog_variant = models.ForeignKey('catalog.ProductVariant', on_delete=models.CASCADE)

    class Meta:
        unique_together = ('owner', 'etsy_variant_name')
        indexes = [models.Index(fields=['owner', 'etsy_variant_name'])]
