from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


TX_TYPES = (
    ('topup', 'Top-up'),
    ('order', 'Order Charge'),
    ('label', 'Shipping Label'),
    ('refund', 'Refund'),
    ('adjustment', 'Adjustment'),
)

TX_STATUS = (
    ('pending', 'Pending'),
    ('completed', 'Completed'),
    ('failed', 'Failed'),
)


class Transaction(TimeStampedModel):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='transactions', on_delete=models.CASCADE)
    tx_type = models.CharField(max_length=20, choices=TX_TYPES)
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text='Negative for charges')
    status = models.CharField(max_length=20, choices=TX_STATUS, default='pending')
    reference = models.CharField(max_length=120, blank=True, help_text='External payment ref or order id')
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ('-created_at',)
