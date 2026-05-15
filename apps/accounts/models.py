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

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='print_center_profile'
    )
    company_name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    max_sub_users = models.PositiveIntegerField(null=True, blank=True)  # null = unlimited
    timezone = models.CharField(max_length=64, default='UTC')

    def __str__(self):
        return self.company_name

    @property
    def can_create_sub_user(self):
        if self.max_sub_users is None:
            return True
        return self.sub_users.count() < self.max_sub_users


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

    # Auto-matching settings (toggled by the customer)
    auto_product_matching = models.BooleanField(default=True)
    auto_design_matching = models.BooleanField(default=True)
    auto_shipping_rules = models.BooleanField(default=True)

    # assigned_stores: ManyToMany → Store (added in shipstation app migration)

    def __str__(self):
        return f"{self.user.email} ({self.print_center.company_name})"

    def can_afford(self, amount):
        import datetime
        if self.credit_until and self.credit_until >= datetime.date.today():
            return True
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

    sub_user = models.ForeignKey(
        SubUserProfile, on_delete=models.CASCADE, related_name='transactions'
    )
    type = models.CharField(max_length=20, choices=Type.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    item_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shipping_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    description = models.TextField(blank=True)
    order_reference = models.CharField(max_length=128, blank=True)  # ShipStation order ID; FK added later
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='balance_transactions'
    )

    def __str__(self):
        return f"{self.get_type_display()} {self.amount} → {self.sub_user}"
