from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel

# Only the original order can be reshipped, never a reship's own clone — and
# only up to this many times (rejected requests don't count against it).
# Shared by apps/reship/views.py (enforcement) and apps/orders/serializers.py
# (the reship_info badge, so the UI can hide the button proactively).
MAX_RESHIP_REQUESTS_PER_ORDER = 2


class ReshipRequest(TimeStampedModel):
    class RequestType(models.TextChoices):
        RESHIP = 'reship', 'Reship'
        REFUND = 'refund', 'Refund'

    class FaultType(models.TextChoices):
        SELF = 'self', 'Buyer/Seller Mistake'
        PC = 'pc', 'Print Center Mistake'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    class Reason(models.TextChoices):
        WRONG_PRODUCT = 'wrong_product', 'Wrong product/color/size sent'
        BAD_PRINT = 'bad_print', 'Wrong or missing print'
        PACKAGE_MIXUP = 'package_mixup', 'Package mix-up'
        DEFECTIVE = 'defective', 'Defective item'
        LOST_SHIPMENT = 'lost_shipment', 'Lost shipment'

    original_order = models.ForeignKey(
        'orders.Order', on_delete=models.CASCADE, related_name='reship_requests',
    )
    sub_user = models.ForeignKey(
        'accounts.SubUserProfile', on_delete=models.CASCADE, related_name='reship_requests',
    )
    request_type = models.CharField(max_length=10, choices=RequestType.choices, default=RequestType.RESHIP)
    fault_type = models.CharField(max_length=10, choices=FaultType.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    # The PC-fault reason for the whole request — items can carry their own
    # too (ReshipRequestItem.reason) when they differ, but the request-level
    # value is what drives the $0 BalanceTransaction description.
    reason = models.CharField(max_length=20, choices=Reason.choices, blank=True)
    note = models.TextField(blank=True)
    auto_approved = models.BooleanField(
        default=False,
        help_text='True when approval was skipped via can_reship_without_pc_approval.',
    )

    new_order = models.ForeignKey(
        'orders.Order', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='spawned_by_reship_request',
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='resolved_reship_requests',
    )
    rejection_note = models.TextField(blank=True)

    # Set once a Refund request is approved — the exact amount credited to the
    # sub-user's balance (item cost + extra-print fee for the selected items,
    # plus shipping only when every billable item on the order was selected).
    # Null for a pending/rejected refund, and always null for a reship.
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    # Optional corrected ship-to address, set at request time. Only the keys the
    # sub-user actually changed are present — _resolve_approved() falls back to
    # the original order's value for anything missing. None = no correction.
    address_override = models.JSONField(null=True, blank=True)

    # Set once _resolve_approved() has attempted the real ShipStation push —
    # lets the UI show a persistent success/failure state instead of only a
    # one-off toast, and lets a failed push be retried without re-cloning.
    shipstation_synced = models.BooleanField(default=False)
    shipstation_sync_error = models.TextField(blank=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'Reship of {self.original_order} ({self.get_status_display()})'


class ReshipRequestItem(TimeStampedModel):
    reship_request = models.ForeignKey(
        ReshipRequest, on_delete=models.CASCADE, related_name='items',
    )
    original_order_item = models.ForeignKey(
        'orders.OrderItem', on_delete=models.CASCADE, related_name='reship_request_items',
    )
    quantity = models.PositiveIntegerField()
    reason = models.CharField(
        max_length=20, choices=ReshipRequest.Reason.choices, blank=True,
    )
    # None = keep the original item's personalization text as-is.
    # '' (empty string) = the sub-user deliberately cleared it.
    personalization_override = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ('id',)

    def __str__(self):
        return f'{self.quantity} × {self.original_order_item} (reship)'


class ReshipRequestFile(TimeStampedModel):
    reship_request = models.ForeignKey(
        ReshipRequest, on_delete=models.CASCADE, related_name='files',
    )
    file = models.FileField(upload_to='reship_evidence/')

    def __str__(self):
        return self.file.name
