from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models

from apps.core.models import TimeStampedModel


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError('Email is required')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        extra_fields.setdefault('role', User.Role.SUB_USER)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', User.Role.SUPERADMIN)
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    class Role(models.TextChoices):
        SUPERADMIN = 'superadmin', 'Super Admin'
        PRINT_CENTER = 'print_center', 'Print Center'
        SUB_USER = 'sub_user', 'Sub User'

    username = None
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=120, blank=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.SUB_USER)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []
    objects = UserManager()

    def __str__(self):
        return self.email

    @property
    def is_superadmin(self):
        return self.role == self.Role.SUPERADMIN

    @property
    def is_print_center(self):
        return self.role == self.Role.PRINT_CENTER

    @property
    def is_sub_user(self):
        return self.role == self.Role.SUB_USER


class PrintCenterProfile(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACTIVE = 'active', 'Active'
        SUSPENDED = 'suspended', 'Suspended'

    class ShippingPricingMode(models.TextChoices):
        DYNAMIC = 'dynamic', 'Dynamic (live rate + markup)'
        FIXED = 'fixed', 'Fixed (weight-based price table)'

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='print_center_profile'
    )
    company_name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    max_sub_users = models.PositiveIntegerField(null=True, blank=True)  # null = unlimited
    timezone = models.CharField(max_length=64, default='UTC')
    pending_timezone = models.CharField(
        max_length=64, blank=True,
        help_text='Requested timezone, applied by generate_monthly_invoices() on the 1st so past '
                  'transactions never shift retroactively.',
    )
    logo = models.ImageField(upload_to='print_center_logos/', null=True, blank=True)
    monthly_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Ship-from address (physical location orders are sent from)
    ship_from_name       = models.CharField(max_length=200, blank=True)
    ship_from_phone      = models.CharField(max_length=30,  blank=True)
    ship_from_street1    = models.CharField(max_length=200, blank=True)
    ship_from_street2    = models.CharField(max_length=200, blank=True)
    ship_from_city       = models.CharField(max_length=120, blank=True)
    ship_from_state      = models.CharField(max_length=80,  blank=True)
    ship_from_postal_code = models.CharField(max_length=20, blank=True)
    ship_from_country    = models.CharField(max_length=2,   blank=True, default='US')

    # Sub-user-facing shipping price source: live cross-provider rate + markup,
    # or a print-center-defined flat table by weight (see FixedShippingRate).
    # Independent of which provider actually buys the label at production time —
    # that comparison always runs to minimize real cost regardless of this mode.
    shipping_pricing_mode = models.CharField(
        max_length=10, choices=ShippingPricingMode.choices, default=ShippingPricingMode.DYNAMIC,
    )

    def __str__(self):
        return self.company_name

    @property
    def can_create_sub_user(self):
        if self.max_sub_users is None:
            return True
        return self.sub_users.count() < self.max_sub_users


class PrintCenterPricingTier(TimeStampedModel):
    """Configurable item-count based extra fee tiers per print center."""
    print_center = models.ForeignKey(
        PrintCenterProfile, on_delete=models.CASCADE, related_name='pricing_tiers'
    )
    min_items = models.PositiveIntegerField()
    max_items = models.PositiveIntegerField(null=True, blank=True)  # null = unlimited
    extra_fee = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ['min_items']

    def __str__(self):
        upper = self.max_items or '∞'
        return f"{self.print_center.company_name}: {self.min_items}–{upper} → +${self.extra_fee}"


class PrintCenterInvoice(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'
        OVERDUE = 'overdue', 'Overdue'

    print_center = models.ForeignKey(
        PrintCenterProfile, on_delete=models.CASCADE, related_name='invoices'
    )
    period_year = models.PositiveIntegerField()
    period_month = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    due_date = models.DateField()
    paid_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        unique_together = ('print_center', 'period_year', 'period_month')
        ordering = ['-period_year', '-period_month']

    def __str__(self):
        return f"{self.print_center.company_name} {self.period_year}/{self.period_month:02d}"


class SubUserInvoice(TimeStampedModel):
    """Mirrors PrintCenterInvoice one level down: a sub-user's monthly bill from
    their print center, generated from that month's BalanceTransaction activity."""
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'
        OVERDUE = 'overdue', 'Overdue'

    sub_user = models.ForeignKey(
        'SubUserProfile', on_delete=models.CASCADE, related_name='invoices'
    )
    period_year = models.PositiveIntegerField()
    period_month = models.PositiveIntegerField()
    order_count = models.PositiveIntegerField(default=0)
    item_count = models.PositiveIntegerField(default=0)          # real unit quantity, see BalanceTransaction.quantity
    item_charged = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    label_count = models.PositiveIntegerField(default=0)         # shipping-category lines, i.e. labels bought
    shipping_charged = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    extra_print_count = models.PositiveIntegerField(default=0)
    extra_print_charged = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gross_charged = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # = item + shipping + extra_print
    credits_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    due_date = models.DateField()
    paid_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        unique_together = ('sub_user', 'period_year', 'period_month')
        ordering = ['-period_year', '-period_month']

    def __str__(self):
        return f"{self.sub_user.user.email} {self.period_year}/{self.period_month:02d}"


class ImpersonationLog(TimeStampedModel):
    """Audit trail: which super admin accessed which print center account."""
    admin = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='impersonation_logs'
    )
    print_center = models.ForeignKey(
        PrintCenterProfile, on_delete=models.CASCADE, related_name='impersonation_logs'
    )

    def __str__(self):
        return f"{self.admin.email} → {self.print_center.company_name}"


class SubUserProfile(TimeStampedModel):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='sub_user_profile'
    )
    print_center = models.ForeignKey(
        PrintCenterProfile, on_delete=models.CASCADE, related_name='sub_users'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='created_sub_users'
    )
    is_active = models.BooleanField(default=True)

    # Timezone — set by the print center (the sub-user only sees it, read-only).
    # Changes are deferred the same way as PrintCenterProfile.pending_timezone.
    timezone = models.CharField(max_length=64, default='UTC')
    pending_timezone = models.CharField(max_length=64, blank=True)

    # Balance
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    negative_balance_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    credit_until = models.DateField(null=True, blank=True)

    # Shipping markup (set by print center per customer)
    shipping_markup_type = models.CharField(
        max_length=10,
        choices=[('fixed', 'Fixed'), ('percent', 'Percent')],
        default='fixed',
    )
    shipping_markup_value = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    # Extra print area pricing (set by print center per customer)
    extra_print_area_enabled = models.BooleanField(default=False)
    extra_print_area_price = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    # Granular permissions
    can_edit_orders = models.BooleanField(default=True)
    can_upload_product_mapping = models.BooleanField(default=True)
    can_upload_designs = models.BooleanField(default=True)
    can_send_to_print = models.BooleanField(default=True)
    can_track_orders = models.BooleanField(default=True)
    can_view_financials = models.BooleanField(default=True)
    can_export_financials = models.BooleanField(default=True)
    can_reship_without_pc_approval = models.BooleanField(
        default=False,
        help_text='A print-center-fault reship from this sub-user skips PC approval and is '
                  'processed immediately, like a self-fault reship.',
    )
    can_report_lost_shipment = models.BooleanField(
        default=False,
        help_text='Whether "Lost Shipment" appears as a reship reason for this sub-user.',
    )

    # Auto-matching settings (toggled by the customer)
    auto_product_matching = models.BooleanField(default=True)
    auto_design_matching = models.BooleanField(default=True)
    auto_shipping_rules = models.BooleanField(default=True)

    # assigned_stores: ManyToMany → Store (added in shipstation app migration)

    def __str__(self):
        return f"{self.user.email} ({self.print_center.company_name})"

    def can_afford(self, amount):
        return (self.balance - amount) >= -abs(self.negative_balance_limit)


class ShippingAutoRule(TimeStampedModel):
    """Maps a marketplace shipping label to a carrier service code."""
    sub_user = models.ForeignKey(
        SubUserProfile, on_delete=models.CASCADE, related_name='shipping_rules'
    )
    marketplace_service = models.CharField(max_length=100)  # e.g. "Etsy Standard"
    carrier_service = models.CharField(max_length=100)      # e.g. "usps_ground_advantage"

    class Meta:
        unique_together = ('sub_user', 'marketplace_service')

    def __str__(self):
        return f"{self.marketplace_service} → {self.carrier_service}"


class BalanceTransaction(TimeStampedModel):
    class Type(models.TextChoices):
        CREDIT = 'credit', 'Credit'
        DEBIT = 'debit', 'Debit'
        REFUND = 'refund', 'Refund'
        ADJUSTMENT = 'adjustment', 'Adjustment'

    class Category(models.TextChoices):
        FULFILLMENT = 'fulfillment', 'Fulfillment'
        SHIPPING = 'shipping', 'Shipping'
        EXTRA_PRINT = 'extra_print', 'Additional Print'
        MANUAL_ADJUSTMENT = 'manual_adjustment', 'Manual Adjustment'
        INVOICE_PAYMENT = 'invoice_payment', 'Invoice Payment'

    sub_user = models.ForeignKey(
        SubUserProfile, on_delete=models.CASCADE, related_name='transactions'
    )
    type = models.CharField(max_length=20, choices=Type.choices)
    category = models.CharField(max_length=20, choices=Category.choices, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    item_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shipping_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    product_name = models.CharField(max_length=300, blank=True)
    size = models.CharField(max_length=20, blank=True)
    color = models.CharField(max_length=40, blank=True)
    quantity = models.PositiveIntegerField(
        default=1,
        help_text='How many physical units this fulfillment line covers — the real basis '
                  'for "item count" (SKU/line count is a different, less useful number).',
    )
    description = models.TextField(blank=True)
    order_reference = models.CharField(max_length=128, blank=True)  # ShipStation order ID (display only)
    order_number = models.CharField(max_length=64, blank=True)  # human-friendly order number (order.shipstation_order_number)
    order = models.ForeignKey(
        'orders.Order', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='balance_transactions',
        help_text='Links a debit to the order it charged for, so a later recall/cancel '
                  'can refund exactly the outstanding amount for that order.',
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='balance_transactions'
    )

    @property
    def display_id(self):
        """Human-friendly 9-digit transaction number, derived from the PK (no separate counter needed)."""
        return 100_000_000 + self.pk

    def __str__(self):
        return f"{self.get_type_display()} {self.amount} → {self.sub_user}"


class Notification(TimeStampedModel):
    """A simple in-app notification for one user — powers the top-bar bell.
    Generic/reusable: any feature can create one, not just reship requests."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    link = models.CharField(
        max_length=300, blank=True,
        help_text='Frontend route path to open when clicked, e.g. "/reship-requests/42".',
    )
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ('-created_at',)
        indexes = [models.Index(fields=['user', 'is_read'])]

    def __str__(self):
        return f'{self.title} → {self.user.email}'
