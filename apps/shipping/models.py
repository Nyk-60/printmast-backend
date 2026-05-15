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

    class Meta:
        unique_together = ('shipstation_account', 'shipstation_store_id')

    def __str__(self):
        return f'{self.name} ({self.shipstation_account.print_center.company_name})'

    @property
    def print_center(self):
        return self.shipstation_account.print_center
