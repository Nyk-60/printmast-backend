import csv
import io
import sys
from decimal import Decimal

from django.db.models import Q, Sum
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import BalanceTransaction, SubUserProfile
from apps.accounts.permissions import IsPrintCenter

from .serializers import (
    BalanceSummarySerializer,
    BalanceTransactionSerializer,
    SubUserBalanceSerializer,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date_range(request):
    date_from = request.query_params.get('from')
    date_to = request.query_params.get('to')
    try:
        from datetime import date
        date_from = date.fromisoformat(date_from) if date_from else None
        date_to = date.fromisoformat(date_to) if date_to else None
    except ValueError:
        date_from = date_to = None
    return date_from, date_to


def _filter_transactions(qs, date_from, date_to, tx_type=None):
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    if tx_type:
        qs = qs.filter(type=tx_type)
    return qs


def _compute_summary(sub_profile, qs_all):
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def agg(type_):
        return qs_all.filter(type=type_).aggregate(s=Sum('amount'))['s'] or Decimal('0')

    month_qs = qs_all.filter(created_at__gte=month_start)

    return {
        'current_balance': sub_profile.balance,
        'total_credited': agg(BalanceTransaction.Type.CREDIT),
        'total_debited': agg(BalanceTransaction.Type.DEBIT),
        'total_refunded': agg(BalanceTransaction.Type.REFUND),
        'this_month_credited': month_qs.filter(
            type=BalanceTransaction.Type.CREDIT
        ).aggregate(s=Sum('amount'))['s'] or Decimal('0'),
        'this_month_debited': month_qs.filter(
            type=BalanceTransaction.Type.DEBIT
        ).aggregate(s=Sum('amount'))['s'] or Decimal('0'),
        'transaction_count': qs_all.count(),
    }


def _build_csv_bytes(qs):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'Date', 'Type', 'Amount', 'Item Amount', 'Shipping Amount',
        'Description', 'Order Reference', 'Created By',
    ])
    for tx in qs:
        by = ''
        try:
            by = tx.created_by.email if tx.created_by_id else ''
        except Exception:
            by = ''
        writer.writerow([
            tx.created_at.strftime('%Y-%m-%d %H:%M'),
            tx.get_type_display(),
            str(tx.amount),
            str(tx.item_amount),
            str(tx.shipping_amount),
            tx.description,
            tx.order_reference,
            by,
        ])
    return buf.getvalue().encode('utf-8-sig')  # UTF-8 BOM for Excel compatibility


def _build_xlsx_bytes(qs):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Transactions'

    headers = [
        'Date', 'Type', 'Amount', 'Item Amount', 'Shipping Amount',
        'Description', 'Order Reference', 'Created By',
    ]
    header_fill = PatternFill(start_color='222222', end_color='222222', fill_type='solid')
    header_font = Font(color='FFFFFF', bold=True)

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')

    for row_idx, tx in enumerate(qs, start=2):
        by = ''
        try:
            by = tx.created_by.email if tx.created_by_id else ''
        except Exception:
            by = ''
        ws.append([
            tx.created_at.strftime('%Y-%m-%d %H:%M'),
            tx.get_type_display(),
            float(tx.amount),
            float(tx.item_amount),
            float(tx.shipping_amount),
            tx.description,
            tx.order_reference,
            by,
        ])
        fill_color = None
        if tx.type == BalanceTransaction.Type.DEBIT:
            fill_color = 'FFE5E5'
        elif tx.type == BalanceTransaction.Type.CREDIT:
            fill_color = 'E5FFE5'
        elif tx.type == BalanceTransaction.Type.REFUND:
            fill_color = 'E5E5FF'
        if fill_color:
            fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type='solid')
            for col in range(1, len(headers) + 1):
                ws.cell(row=row_idx, column=col).fill = fill

    for col in range(1, len(headers) + 1):
        max_len = max(
            len(str(ws.cell(row=r, column=col).value or ''))
            for r in range(1, ws.max_row + 1)
        )
        ws.column_dimensions[get_column_letter(col)].width = min(max_len + 4, 40)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


def _make_export_response(qs, fmt, filename_base):
    """Build an HttpResponse for CSV or XLSX download."""
    if fmt == 'xlsx':
        data = _build_xlsx_bytes(qs)
        ct = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        fname = f'{filename_base}.xlsx'
    else:
        data = _build_csv_bytes(qs)
        ct = 'text/csv; charset=utf-8-sig'
        fname = f'{filename_base}.csv'

    resp = HttpResponse(data, content_type=ct, status=200)
    resp['Content-Disposition'] = f'attachment; filename="{fname}"'
    resp['Content-Length'] = len(data)
    return resp


# ── Sub-user views ────────────────────────────────────────────────────────────

class MySummaryView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        sub = request.user.sub_user_profile
        qs = BalanceTransaction.objects.filter(sub_user=sub)
        data = _compute_summary(sub, qs)
        return Response(BalanceSummarySerializer(data).data)


class MyTransactionListView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        sub = request.user.sub_user_profile
        date_from, date_to = _parse_date_range(request)
        tx_type = request.query_params.get('type')

        qs = BalanceTransaction.objects.filter(sub_user=sub).select_related('created_by')
        qs = _filter_transactions(qs, date_from, date_to, tx_type).order_by('-created_at')

        page = int(request.query_params.get('page', 1))
        page_size = min(int(request.query_params.get('page_size', 50)), 200)
        total = qs.count()
        qs_page = qs[(page - 1) * page_size: page * page_size]

        return Response({
            'count': total,
            'page': page,
            'page_size': page_size,
            'results': BalanceTransactionSerializer(qs_page, many=True).data,
        })


class _ExportMixin:
    """Mixin that bypasses DRF's format-based content negotiation.

    When a client sends ?format=csv, DRF tries to find a CSV renderer and
    raises Http404 if none is registered. We render the file ourselves, so
    we force-select the first available renderer to skip that check.
    """
    def perform_content_negotiation(self, request, force=False):
        return super().perform_content_negotiation(request, force=True)


class MyTransactionExportView(_ExportMixin, APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        sub = request.user.sub_user_profile
        date_from, date_to = _parse_date_range(request)
        fmt = request.query_params.get('format', 'csv').lower()
        suffix = f'{date_from or "all"}_{date_to or "all"}'

        qs = BalanceTransaction.objects.filter(sub_user=sub).select_related('created_by')
        qs = _filter_transactions(qs, date_from, date_to).order_by('-created_at')

        return _make_export_response(qs, fmt, f'transactions_{suffix}')


# ── Print Center views ────────────────────────────────────────────────────────

class FinanceOverviewView(APIView):
    permission_classes = (IsPrintCenter,)

    def get(self, request):
        pc = request.user.print_center_profile
        sub_users = SubUserProfile.objects.filter(
            print_center=pc
        ).select_related('user').prefetch_related('transactions')

        result = []
        for sub in sub_users:
            txs = sub.transactions.all()
            result.append({
                'sub_user_id': sub.pk,
                'email': sub.user.email,
                'full_name': sub.user.full_name,
                'balance': sub.balance,
                'negative_balance_limit': sub.negative_balance_limit,
                'is_active': sub.is_active,
                'total_credited': txs.filter(type=BalanceTransaction.Type.CREDIT).aggregate(
                    s=Sum('amount'))['s'] or Decimal('0'),
                'total_debited': txs.filter(type=BalanceTransaction.Type.DEBIT).aggregate(
                    s=Sum('amount'))['s'] or Decimal('0'),
            })

        return Response(SubUserBalanceSerializer(result, many=True).data)


class SubUserSummaryView(APIView):
    permission_classes = (IsPrintCenter,)

    def _get_sub(self, request, pk):
        try:
            return SubUserProfile.objects.get(pk=pk, print_center=request.user.print_center_profile)
        except SubUserProfile.DoesNotExist:
            raise NotFound()

    def get(self, request, pk):
        sub = self._get_sub(request, pk)
        qs = BalanceTransaction.objects.filter(sub_user=sub)
        data = _compute_summary(sub, qs)
        return Response(BalanceSummarySerializer(data).data)


class SubUserTransactionListView(APIView):
    permission_classes = (IsPrintCenter,)

    def get(self, request, pk):
        try:
            sub = SubUserProfile.objects.get(pk=pk, print_center=request.user.print_center_profile)
        except SubUserProfile.DoesNotExist:
            raise NotFound()

        date_from, date_to = _parse_date_range(request)
        tx_type = request.query_params.get('type')

        qs = BalanceTransaction.objects.filter(sub_user=sub).select_related('created_by')
        qs = _filter_transactions(qs, date_from, date_to, tx_type).order_by('-created_at')

        page = int(request.query_params.get('page', 1))
        page_size = min(int(request.query_params.get('page_size', 50)), 200)
        total = qs.count()
        qs_page = qs[(page - 1) * page_size: page * page_size]

        return Response({
            'count': total,
            'page': page,
            'page_size': page_size,
            'results': BalanceTransactionSerializer(qs_page, many=True).data,
        })


class SubUserTransactionExportView(_ExportMixin, APIView):
    permission_classes = (IsPrintCenter,)

    def get(self, request, pk):
        try:
            sub = SubUserProfile.objects.get(pk=pk, print_center=request.user.print_center_profile)
        except SubUserProfile.DoesNotExist:
            raise NotFound()

        date_from, date_to = _parse_date_range(request)
        fmt = request.query_params.get('format', 'csv').lower()
        suffix = f'{sub.user.email}_{date_from or "all"}_{date_to or "all"}'

        qs = BalanceTransaction.objects.filter(sub_user=sub).select_related('created_by')
        qs = _filter_transactions(qs, date_from, date_to).order_by('-created_at')

        return _make_export_response(qs, fmt, f'transactions_{suffix}')
