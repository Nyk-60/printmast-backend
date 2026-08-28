from django.db import models

from apps.core.encryption import EncryptedCharField
from apps.core.models import TimeStampedModel


class ShipStationCarrier(TimeStampedModel):
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class ShipStationAccount(TimeStampedModel):
    """A print center's connected ShipStation account."""

    class ApiVersion(models.TextChoices):
        V1 = 'V1', 'V1 (API Key + Secret)'
        V2 = 'V2', 'V2 (API Key only)'

    print_center = models.ForeignKey(
        'accounts.PrintCenterProfile',
        related_name='shipstation_accounts',
        on_delete=models.CASCADE,
    )
    label = models.CharField(max_length=100, help_text='Display name for this account')
    api_version = models.CharField(max_length=2, choices=ApiVersion.choices, default=ApiVersion.V1)
    api_key = EncryptedCharField()
    api_secret = EncryptedCharField(blank=True, default='')  # V2 does not use secret
    is_active = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'{self.label} ({self.print_center.company_name})'


class Store(TimeStampedModel):
    """A ShipStation store (sales channel) belonging to a ShipStation account."""

    shipstation_account = models.ForeignKey(
        ShipStationAccount, related_name='stores', on_delete=models.CASCADE
    )
    shipstation_store_id = models.CharField(max_length=64, db_index=True)
    name = models.CharField(max_length=200)
    marketplace = models.CharField(max_length=80, blank=True)  # Etsy, Shopify, WooCommerce…
    marketplace_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)

    # Sub-users assigned to this store
    assigned_sub_users = models.ManyToManyField(
        'accounts.SubUserProfile',
        related_name='assigned_stores',
        blank=True,
    )

    # Seller-set sender info shown on shipping labels for this store
    ship_from_name    = models.CharField(max_length=200, blank=True,
        help_text='Sender name on labels for this store (e.g. "MyStore"). Falls back to PC company name.')
    ship_from_company = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = ('shipstation_account', 'shipstation_store_id')

    def __str__(self):
        return f'{self.name} ({self.shipstation_account.print_center.company_name})'

    @property
    def print_center(self):
        return self.shipstation_account.print_center


class ShippingServiceOption(TimeStampedModel):
    """A carrier+service combo the print center offers, grouped into a category."""

    class Category(models.TextChoices):
        STANDARD = 'standard', 'Standard'
        PRIORITY = 'priority', 'Priority'
        EXPRESS = 'express', 'Express'

    print_center = models.ForeignKey(
        'accounts.PrintCenterProfile',
        related_name='shipping_service_options',
        on_delete=models.CASCADE,
    )
    category = models.CharField(max_length=10, choices=Category.choices)
    carrier_code = models.CharField(max_length=80)
    service_code = models.CharField(max_length=80)
    service_name = models.CharField(max_length=120, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ('category', 'sort_order', 'id')
        unique_together = ('print_center', 'category', 'carrier_code', 'service_code')

    def __str__(self):
        return f'{self.print_center.company_name} / {self.category} / {self.service_code}'


class ShippingServiceDefault(TimeStampedModel):
    """A sub-user's default service choice for a category.

    Constrained to the print center's ShippingServiceOption entries —
    sub-users cannot set arbitrary carrier/service codes.
    """

    sub_user = models.ForeignKey(
        'accounts.SubUserProfile',
        related_name='shipping_service_defaults',
        on_delete=models.CASCADE,
    )
    category = models.CharField(max_length=10, choices=ShippingServiceOption.Category.choices)
    service_option = models.ForeignKey(
        ShippingServiceOption,
        null=True, blank=True,
        related_name='+',
        on_delete=models.SET_NULL,
        help_text="Null = use the cheapest of the print center's options for this category.",
    )

    class Meta:
        unique_together = ('sub_user', 'category')

    def __str__(self):
        target = self.service_option.service_code if self.service_option else 'cheapest'
        return f'{self.sub_user} / {self.category} -> {target}'
