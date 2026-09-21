from django.db import models

from apps.core.models import TimeStampedModel

SHEET_LENGTH_CHOICES = [(v, f'{v}"') for v in (100, 200, 250, 300, 350, 400)]


class ProductionBatch(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        LABELING = 'labeling', 'Buying labels'
        NEEDS_ATTENTION = 'needs_attention', 'Needs attention'
        PROCESSING = 'processing', 'Building sheets'   # layout / render / slips
        READY = 'ready', 'Ready'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    print_center = models.ForeignKey(
        'accounts.PrintCenterProfile', related_name='batches', on_delete=models.PROTECT
    )
    created_by = models.ForeignKey(
        'accounts.User', related_name='batches', on_delete=models.PROTECT
    )
    sheet_length_inches = models.PositiveIntegerField(
        choices=SHEET_LENGTH_CHOICES, default=200
    )
    gang_sheet_dpi = models.PositiveIntegerField(default=300)
    margin_inches = models.DecimalField(max_digits=4, decimal_places=2, default='0.20')
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    error_message = models.TextField(blank=True)
    # Set when the label pass stopped on purpose because of an account-level
    # problem (e.g. provider balance) that would fail every remaining order.
    paused_reason = models.TextField(blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'Batch #{self.pk} — {self.print_center.company_name} ({self.status})'


class BatchOrder(TimeStampedModel):
    """Links a production batch to a specific order with a sequence number.

    Each order moves through the label pass on its own, so one bad order never
    blocks or invalidates the others: pending -> labeled | failed | needs_review,
    or removed when the print center pulls it back out of the batch.
    """

    class LabelStatus(models.TextChoices):
        PENDING = 'pending', 'Waiting'
        LABELED = 'labeled', 'Labeled'
        FAILED = 'failed', 'Failed'
        NEEDS_REVIEW = 'needs_review', 'Needs review'
        REMOVED = 'removed', 'Removed'

    class ErrorKind(models.TextChoices):
        DATA = 'data', 'Order data (fix and retry)'
        ACCOUNT = 'account', 'Provider account'
        TRANSIENT = 'transient', 'Temporary'
        AMBIGUOUS = 'ambiguous', 'Outcome unknown'
        OTHER = 'other', 'Other'

    batch = models.ForeignKey(ProductionBatch, related_name='batch_orders', on_delete=models.CASCADE)
    order = models.ForeignKey('orders.Order', related_name='batch_orders', on_delete=models.PROTECT)
    # Page number / design group label. Assigned when the sheets are built, once the
    # set of labeled orders is final, so numbering never has gaps.
    sequence_number = models.PositiveIntegerField(default=0)
    label_status = models.CharField(
        max_length=14, choices=LabelStatus.choices, default=LabelStatus.PENDING, db_index=True,
    )
    error_kind = models.CharField(max_length=10, choices=ErrorKind.choices, blank=True)
    error_message = models.TextField(blank=True)
    label_purchased = models.BooleanField(default=False)
    label_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        unique_together = ('batch', 'order')
        ordering = ('sequence_number', 'id')

    def __str__(self):
        return f'#{self.sequence_number} — {self.order}'


class GangSheet(TimeStampedModel):
    """One gang sheet PNG file within a batch."""
    batch = models.ForeignKey(ProductionBatch, related_name='gang_sheets', on_delete=models.CASCADE)
    sheet_number = models.PositiveIntegerField()
    file = models.FileField(upload_to='production/gang_sheets/')
    width_inches = models.DecimalField(max_digits=6, decimal_places=2, default=22)
    length_inches = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    class Meta:
        ordering = ('sheet_number',)

    def __str__(self):
        return f'Batch #{self.batch_id} — Sheet {self.sheet_number}'


class OrderSlipPDF(TimeStampedModel):
    """One order-slip PDF file, paired 1-to-1 with a GangSheet."""
    batch = models.ForeignKey(ProductionBatch, related_name='slip_pdfs', on_delete=models.CASCADE)
    gang_sheet = models.OneToOneField(GangSheet, related_name='slip_pdf', on_delete=models.CASCADE)
    file = models.FileField(upload_to='production/slips/')

    def __str__(self):
        return f'Slip PDF for {self.gang_sheet}'
