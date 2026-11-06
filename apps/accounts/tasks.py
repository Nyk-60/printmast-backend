"""Monthly billing cycle: applies deferred timezone changes, generates print-center
and sub-user invoices for the month that just ended, and marks overdue invoices."""
import logging
from datetime import timedelta
from decimal import Decimal

from celery import shared_task
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def generate_monthly_invoices():
    """Runs ayın 1'i (see CELERY_BEAT_SCHEDULE). Idempotent — safe to re-run."""
    from apps.accounts.models import (
        BalanceTransaction, PrintCenterInvoice, PrintCenterProfile,
        SubUserInvoice, SubUserProfile,
    )

    # 1. Apply any pending timezone cutover before anything else this run.
    for pc in PrintCenterProfile.objects.exclude(pending_timezone=''):
        pc.timezone, pc.pending_timezone = pc.pending_timezone, ''
        pc.save(update_fields=['timezone', 'pending_timezone'])
    for su in SubUserProfile.objects.exclude(pending_timezone=''):
        su.timezone, su.pending_timezone = su.pending_timezone, ''
        su.save(update_fields=['timezone', 'pending_timezone'])

    today = timezone.now().date()
    prior_month_end = today.replace(day=1) - timedelta(days=1)
    period_start = prior_month_end.replace(day=1)
    year, month = prior_month_end.year, prior_month_end.month
    due_date = today.replace(day=5)

    # 2. Print-center monthly fee invoices.
    for pc in PrintCenterProfile.objects.filter(
        monthly_fee__gt=0, status=PrintCenterProfile.Status.ACTIVE
    ):
        PrintCenterInvoice.objects.get_or_create(
            print_center=pc, period_year=year, period_month=month,
            defaults={'amount': pc.monthly_fee, 'due_date': due_date},
        )

    # 3. Sub-user invoices, aggregated from that month's BalanceTransaction activity.
    for su in SubUserProfile.objects.filter(is_active=True):
        qs = BalanceTransaction.objects.filter(
            sub_user=su,
            created_at__date__gte=period_start,
            created_at__date__lte=prior_month_end,
        )
        gross = qs.filter(type=BalanceTransaction.Type.DEBIT).aggregate(
            s=Sum('amount')
        )['s'] or Decimal('0')
        credits = qs.filter(
            type__in=(BalanceTransaction.Type.CREDIT, BalanceTransaction.Type.REFUND)
        ).exclude(category=BalanceTransaction.Category.MANUAL_ADJUSTMENT).aggregate(
            s=Sum('amount')
        )['s'] or Decimal('0')

        if gross == 0 and credits == 0:
            continue  # nothing happened this month — no invoice

        # item_count is the real physical unit count (same or different SKU,
        # doesn't matter) — not how many fulfillment LINES/SKUs there were,
        # which is a different, less useful number for procurement/billing.
        fulfillment_qs = qs.filter(category=BalanceTransaction.Category.FULFILLMENT)
        shipping_qs = qs.filter(category=BalanceTransaction.Category.SHIPPING)
        extra_print_qs = qs.filter(category=BalanceTransaction.Category.EXTRA_PRINT)

        item_qty = fulfillment_qs.aggregate(s=Sum('quantity'))['s'] or 0
        item_charged = fulfillment_qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')
        label_count = shipping_qs.count()
        shipping_charged = shipping_qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')
        extra_print_count = extra_print_qs.count()
        extra_print_charged = extra_print_qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')

        SubUserInvoice.objects.get_or_create(
            sub_user=su, period_year=year, period_month=month,
            defaults={
                'order_count': qs.exclude(order=None).values('order_id').distinct().count(),
                'item_count': item_qty,
                'item_charged': item_charged,
                'label_count': label_count,
                'shipping_charged': shipping_charged,
                'extra_print_count': extra_print_count,
                'extra_print_charged': extra_print_charged,
                'gross_charged': gross,
                'credits_total': credits,
                'amount': max(Decimal('0'), gross - credits),
                'due_date': due_date,
            },
        )


@shared_task
def mark_overdue_invoices():
    """Runs ayın 6'sı (see CELERY_BEAT_SCHEDULE)."""
    from apps.accounts.models import PrintCenterInvoice, SubUserInvoice

    today = timezone.now().date()
    PrintCenterInvoice.objects.filter(status='pending', due_date__lt=today).update(status='overdue')
    SubUserInvoice.objects.filter(status='pending', due_date__lt=today).update(status='overdue')
