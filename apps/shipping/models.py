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

    # Physical ship-from address — synced from this store's ShipStation
    # warehouse (originAddress), NOT editable in our panel: ShipStation is
    # the source of truth here, we only display it for confirmation and use
    # it when purchasing labels through ANY provider (ShipStation or not).
    shipstation_warehouse_id = models.CharField(max_length=64, blank=True)
    ship_from_street1        = models.CharField(max_length=200, blank=True)
    ship_from_street2        = models.CharField(max_length=200, blank=True)
    ship_from_city           = models.CharField(max_length=120, blank=True)
    ship_from_state          = models.CharField(max_length=80,  blank=True)
    ship_from_postal_code    = models.CharField(max_length=20,  blank=True)
    ship_from_country        = models.CharField(max_length=2,   blank=True)
    ship_from_phone          = models.CharField(max_length=30,  blank=True)
    ship_from_synced_at      = models.DateTimeField(null=True, blank=True)

    @property
    def ship_from_configured(self):
        return bool(self.ship_from_postal_code and self.ship_from_city)

    class Meta:
        unique_together = ('shipstation_account', 'shipstation_store_id')

    def __str__(self):
        return f'{self.name} ({self.shipstation_account.print_center.company_name})'

    @property
    def print_center(self):
        return self.shipstation_account.print_center


class LabelProviderAccount(TimeStampedModel):
    """A print center's connected label-purchasing provider.

    Distinct from ShipStationAccount (order source): ShipStation can ALSO be
    used to purchase labels (most print centers already do), so this either
    points at an existing ShipStationAccount, or holds its own credentials
    for a non-ShipStation provider like ByeLabel. Whichever accounts are
    is_active are compared for the cheapest rate at label-purchase time.
    """

    class Provider(models.TextChoices):
        SHIPSTATION = 'shipstation', 'ShipStation'
        BYELABEL = 'byelabel', 'ByeLabel'

    print_center = models.ForeignKey(
        'accounts.PrintCenterProfile',
        related_name='label_provider_accounts',
        on_delete=models.CASCADE,
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    label = models.CharField(max_length=100, help_text='Display name for this connection')
    is_active = models.BooleanField(default=True)

    # provider == SHIPSTATION: reuse an already-connected order-source account
    shipstation_account = models.ForeignKey(
        ShipStationAccount, null=True, blank=True,
        on_delete=models.CASCADE, related_name='label_provider_links',
    )

    # provider == BYELABEL
    byelabel_client_id = EncryptedCharField(blank=True, default='')
    byelabel_client_secret = EncryptedCharField(blank=True, default='')
    byelabel_workspace_id = EncryptedCharField(blank=True, default='')

    class Meta:
        ordering = ('print_center', 'provider', 'label')

    def __str__(self):
        return f'{self.label} ({self.get_provider_display()})'


class ShippingServiceOption(TimeStampedModel):
    """A carrier+service combo the print center offers, grouped into a category.

    Scoped per provider: the same category (e.g. "standard") can list options
    from multiple providers, so the label-purchase rate comparison knows which
    (provider, carrier, service) combinations are acceptable substitutes for
    the service level the sub-user was charged for.
    """

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
    provider = models.CharField(
        max_length=20, choices=LabelProviderAccount.Provider.choices,
        default=LabelProviderAccount.Provider.SHIPSTATION,
    )
    carrier_code = models.CharField(max_length=80)
    service_code = models.CharField(max_length=80)
    service_name = models.CharField(max_length=120, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ('category', 'sort_order', 'id')
        unique_together = ('print_center', 'category', 'provider', 'carrier_code', 'service_code')

    def __str__(self):
        return f'{self.print_center.company_name} / {self.category} / {self.provider} / {self.service_code}'


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


class FixedShippingRate(TimeStampedModel):
    """A print-center-defined flat shipping price for a weight bracket + category.

    Used instead of a live rate + markup when the print center's
    shipping_pricing_mode is 'fixed'. Mirrors PrintCenterPricingTier's
    min/max/value bracket pattern. Brackets are matched by weight only —
    not per-product — since shipping cost is driven by weight/dims, not
    product identity, and a per-product table would be unmaintainable at
    catalog scale.
    """
    print_center = models.ForeignKey(
        'accounts.PrintCenterProfile', related_name='fixed_shipping_rates', on_delete=models.CASCADE,
    )
    category = models.CharField(max_length=10, choices=ShippingServiceOption.Category.choices)
    min_weight_oz = models.DecimalField(max_digits=8, decimal_places=2)
    max_weight_oz = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)  # null = no upper bound
    price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ('category', 'min_weight_oz')

    def __str__(self):
        upper = self.max_weight_oz if self.max_weight_oz is not None else '∞'
        return f'{self.print_center.company_name} / {self.category}: {self.min_weight_oz}–{upper} oz → ${self.price}'


class LabelPurchaseAttempt(TimeStampedModel):
    """Audit trail + idempotency guard for one label-purchase attempt.

    Every attempt (successful or not) gets its own row here — never
    overwritten — so a discrepancy can always be reconstructed. The DB-level
    unique constraint (at most one CONFIRMED row per order) is the final
    backstop against a double purchase even if application logic has a bug.
    """

    class Status(models.TextChoices):
        REQUESTED = 'requested', 'Requested'
        CONFIRMED = 'confirmed', 'Confirmed'
        FAILED = 'failed', 'Failed'
        NEEDS_REVIEW = 'needs_review', 'Needs Manual Review'

    order = models.ForeignKey('orders.Order', related_name='label_attempts', on_delete=models.CASCADE)
    provider_account = models.ForeignKey(
        LabelProviderAccount, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='attempts',
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REQUESTED)
    carrier_code = models.CharField(max_length=80, blank=True)
    service_code = models.CharField(max_length=80, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    tracking_number = models.CharField(max_length=120, blank=True)
    label_url = models.URLField(blank=True)
    error_message = models.TextField(blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    # For labels bought outside ShipStation: when ShipStation was told the order
    # shipped (which is what makes it notify the marketplace with tracking).
    shipstation_notified_at = models.DateTimeField(null=True, blank=True)
    notify_error = models.TextField(blank=True)

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=['order'], condition=models.Q(status='confirmed'),
                name='unique_confirmed_label_per_order',
            ),
        ]

    def __str__(self):
        return f'{self.order} — {self.status}'
