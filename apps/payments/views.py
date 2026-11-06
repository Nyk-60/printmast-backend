import csv
import io
import json
import sys
from contextlib import nullcontext
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Case, DecimalField, F, Q, Sum, When, Window
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import (
    BalanceTransaction, PrintCenterInvoice, SubUserInvoice, SubUserProfile,
)
from apps.accounts.permissions import IsPrintCenter
from apps.accounts.serializers import SubUserInvoiceSerializer

from .serializers import (
    BalanceSummarySerializer,
    BalanceTransactionSerializer,
    SubUserBalanceSerializer,
    pc_time as _pc_time,
    user_time as _user_time,
    user_label as _user_label,
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


def _filter_transactions(qs, date_from, date_to, tx_type=None, tz_name=None):
    """`tz_name` (the owning PC's timezone) makes 'from'/'to' day boundaries
    match that PC's own calendar instead of the server's default (UTC) — and,
    critically, the SAME PC timezone is used whether this is a sub-user
    viewing their own history or the PC viewing that same sub-user, so the
    two views of the identical period always agree."""
    if date_from or date_to:
        with timezone.override(ZoneInfo(tz_name)) if tz_name else nullcontext():
            if date_from:
                qs = qs.filter(created_at__date__gte=date_from)
            if date_to:
                qs = qs.filter(created_at__date__lte=date_to)
    if tx_type:
        qs = qs.filter(type=tx_type)
    return qs


# The Item/Production Report is about what was actually produced and charged
# for — a line per product/shipping/extra-print charge. Refunds, manual
# balance adjustments, and invoice payments aren't production line items (no
# product/size/color), so they're excluded here rather than showing up as
# blank-everything-but-Amount rows.
_ITEM_REPORT_CATEGORIES = (
    BalanceTransaction.Category.FULFILLMENT,
    BalanceTransaction.Category.SHIPPING,
    BalanceTransaction.Category.EXTRA_PRINT,
)


def _apply_report_filter(qs, report):
    if report == 'items':
        return qs.filter(category__in=_ITEM_REPORT_CATEGORIES)
    return qs


# ── Excel-style column filter dropdowns (PC's combined Balance History) ──────

def _parse_csv_list(request, param):
    raw = request.query_params.get(param)
    if not raw:
        return None
    values = [v for v in raw.split(',') if v]
    return values or None


def _parse_json_list(request, param):
    raw = request.query_params.get(param)
    if not raw:
        return None
    try:
        values = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return values if isinstance(values, list) and values else None


def _apply_column_filters(qs, request, pc):
    """Applies the Excel-style per-column checklist filters (and the
    independent Order No search box) a PC can set on their combined Balance
    History table. Each param is optional; absent/empty means 'no filter'."""
    order_no = request.query_params.get('order_no')
    if order_no:
        qs = qs.filter(order_number__icontains=order_no)

    shops = _parse_csv_list(request, 'shop_in')
    if shops:
        qs = qs.filter(order__store__name__in=shops)

    type_codes = _parse_csv_list(request, 'type_in')
    if type_codes:
        cat_codes = [c.split(':', 1)[1] for c in type_codes if c.startswith('cat:')]
        raw_types = [c.split(':', 1)[1] for c in type_codes if c.startswith('type:')]
        cond = Q()
        if cat_codes:
            cond |= Q(category__in=cat_codes)
        if raw_types:
            cond |= Q(type__in=raw_types)
        qs = qs.filter(cond)

    items = _parse_json_list(request, 'item_in')
    if items:
        cond = Q()
        for it in items:
            if not isinstance(it, dict):
                continue
            cond |= Q(
                product_name=it.get('product_name', ''),
                size=it.get('size', ''),
                color=it.get('color', ''),
            )
        qs = qs.filter(cond)

    explanations = _parse_json_list(request, 'explanation_in')
    if explanations:
        qs = qs.filter(description__in=explanations)

    pc_dates = _parse_csv_list(request, 'pc_date_in')
    if pc_dates:
        try:
            parsed = [date.fromisoformat(d) for d in pc_dates]
        except ValueError:
            parsed = []
        if parsed:
            with timezone.override(ZoneInfo(pc.timezone)):
                qs = qs.filter(id__in=list(
                    qs.annotate(_d=TruncDate('created_at')).filter(_d__in=parsed).values_list('id', flat=True)
                ))

    user_dates = _parse_csv_list(request, 'user_date_in')
    if user_dates:
        wanted = set(user_dates)
        matching_ids = []
        for tx in qs.select_related('sub_user').only('id', 'created_at', 'sub_user__timezone'):
            try:
                local_date = tx.created_at.astimezone(ZoneInfo(tx.sub_user.timezone)).date().isoformat()
            except Exception:
                local_date = tx.created_at.date().isoformat()
            if local_date in wanted:
                matching_ids.append(tx.id)
        qs = qs.filter(id__in=matching_ids)

    return qs


def _compute_summary(sub_profile, qs_all, date_from=None, date_to=None):
    """`total_*` and `this_month_*` both now scope to the caller's selected
    period (date_from/date_to — the same Month/Custom Range filter driving
    the table below) rather than a fixed real-world 'this calendar month',
    so the summary tiles always match whatever period is being browsed."""
    qs_period = _filter_transactions(qs_all, date_from, date_to, tz_name=sub_profile.print_center.timezone)

    def agg(type_):
        return qs_period.filter(type=type_).aggregate(s=Sum('amount'))['s'] or Decimal('0')

    return {
        'current_balance': sub_profile.balance,
        'total_credited': agg(BalanceTransaction.Type.CREDIT),
        'total_debited': agg(BalanceTransaction.Type.DEBIT),
        'total_refunded': agg(BalanceTransaction.Type.REFUND),
        'this_month_credited': agg(BalanceTransaction.Type.CREDIT),
        'this_month_debited': agg(BalanceTransaction.Type.DEBIT),
        'transaction_count': qs_period.count(),
        'unpaid_invoice_count': SubUserInvoice.objects.filter(
            sub_user=sub_profile, status__in=('pending', 'overdue'),
        ).count(),
    }


def _signed_amount(tx):
    """Debit = negative (money owed), credit/refund = positive, 0 stays 0 (e.g. a
    PC-fault reship that isn't charged to the user)."""
    return -tx.amount if tx.type == BalanceTransaction.Type.DEBIT else tx.amount


def _tx_type_label(tx):
    # 'category' is the charge kind (Fulfillment/Shipping/Additional Print) and is
    # shown even on a refund/credit row so it's clear *what* was refunded. Rows with
    # no category (e.g. a whole-order recall refund) fall back to the credit/debit type.
    return tx.get_category_display() if tx.category else tx.get_type_display()


def _item_cell(tx):
    parts = [p for p in (tx.product_name, tx.size, tx.color) if p]
    return ' - '.join(parts)


def _signed_amount_expr():
    """DB-level equivalent of `_signed_amount`, for aggregates/window sums."""
    return Case(
        When(type=BalanceTransaction.Type.DEBIT, then=-F('amount')),
        default=F('amount'),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )


def _balance_seed(sub_profile, date_from):
    """Starting point for a from-zero running-balance replay so the final
    figure always lands on the real stored balance.

    A from-zero replay of the ledger can diverge from `sub_profile.balance`
    (e.g. an old manual correction applied directly to the balance, outside
    the transaction ledger) — anchoring every computed running balance to
    this seed guarantees the most recent row always matches the real
    current balance, regardless of date filtering, instead of silently
    drifting from reality."""
    total_all = BalanceTransaction.objects.filter(sub_user=sub_profile).aggregate(
        s=Sum(_signed_amount_expr())
    )['s'] or Decimal('0')
    offset = sub_profile.balance - total_all

    if date_from:
        with timezone.override(ZoneInfo(sub_profile.print_center.timezone)):
            prior = BalanceTransaction.objects.filter(
                sub_user=sub_profile, created_at__date__lt=date_from,
            ).aggregate(s=Sum(_signed_amount_expr()))['s'] or Decimal('0')
    else:
        prior = Decimal('0')

    return offset + prior


def _balance_seed_map(print_center, date_from, sub_user_id=None):
    """Like `_balance_seed`, but for a queryset spanning several sub-users at
    once (the PC's combined ledger): one seed per sub_user_id, so each
    sub-user's own running Balance stays anchored to their own real balance
    regardless of how their rows interleave with everyone else's."""
    if sub_user_id:
        sub = SubUserProfile.objects.get(pk=sub_user_id, print_center=print_center)
        return {sub.pk: _balance_seed(sub, date_from)}
    return {
        sub.pk: _balance_seed(sub, date_from)
        for sub in SubUserProfile.objects.filter(print_center=print_center)
    }


def _balance_seed_combined(pc, date_from):
    """Like `_balance_seed`, but anchors ONE running total across every
    sub-user under this PC combined — this is the PC's own net position
    against its whole customer base (what you see when no single sub-user is
    selected), not any one sub-user's balance."""
    total_real_balance = SubUserProfile.objects.filter(print_center=pc).aggregate(
        s=Sum('balance')
    )['s'] or Decimal('0')
    total_all = BalanceTransaction.objects.filter(sub_user__print_center=pc).aggregate(
        s=Sum(_signed_amount_expr())
    )['s'] or Decimal('0')
    offset = total_real_balance - total_all

    if date_from:
        with timezone.override(ZoneInfo(pc.timezone)):
            prior = BalanceTransaction.objects.filter(
                sub_user__print_center=pc, created_at__date__lt=date_from,
            ).aggregate(s=Sum(_signed_amount_expr()))['s'] or Decimal('0')
    else:
        prior = Decimal('0')

    return offset + prior


def _compute_balance_map(truth_qs, pc, sub_user_id, date_from):
    """id -> true balance_after, computed over `truth_qs` (date/type/report
    scoped only — NOT narrowed by the Excel-style column filters) so the
    figure stays correct no matter which rows a caller later chooses to
    display. Column-filtering a table (e.g. to one Shop) hides some rows,
    which would silently break a running-balance computed only from what's
    left visible — computing it here, against the full picture, first,
    avoids that. `sub_user_id` of None means a PC's combined, multi-sub-user
    `truth_qs`."""
    if sub_user_id:
        annotated = truth_qs.annotate(window_sum=Window(
            expression=Sum(_signed_amount_expr()),
            partition_by=F('sub_user_id'),
            order_by=[F('created_at').asc(), F('id').asc()],
        ))
        seed_map = _balance_seed_map(pc, date_from, sub_user_id)
        seed = seed_map.get(int(sub_user_id), Decimal('0'))
        return {row['id']: row['window_sum'] + seed for row in annotated.values('id', 'window_sum')}

    annotated = truth_qs.annotate(window_sum=Window(
        expression=Sum(_signed_amount_expr()),
        order_by=[F('created_at').asc(), F('id').asc()],
    ))
    seed = _balance_seed_combined(pc, date_from)
    return {row['id']: row['window_sum'] + seed for row in annotated.values('id', 'window_sum')}


def _row_fill_color(tx):
    """Orange = an anomaly (a real credit/refund, or a $0 PC-covered reship).
    Otherwise alternate green/white, one stripe per order (rows sharing the same
    Order No stay the same color; rows with no order — manual adjustments — never
    group with each other)."""
    if tx.type == BalanceTransaction.Type.REFUND or tx.amount == 0:
        return 'FDE9D9'  # orange
    return None  # caller decides the green/white stripe


def _build_balance_history_rows(qs, balance_map):
    """Yields (tx, signed_amount, balance_after) for the Balance History
    report. `balance_map` is a precomputed {tx.id: true balance_after} dict
    (see `_compute_balance_map`) — computed over the full date/sub-user-scoped
    ledger BEFORE any column filter narrowed `qs`, so the figure is correct
    even when `qs` only shows a subset of rows (e.g. one Shop)."""
    for tx in qs:
        yield tx, _signed_amount(tx), balance_map.get(tx.id, Decimal('0'))


def _build_csv_bytes(qs, report, balance_map=None):
    buf = io.StringIO()
    writer = csv.writer(buf)
    if report == 'items':
        writer.writerow([
            'Transaction ID', 'Order No', 'Shop', 'PC Local Time', 'User Local Time',
            'Transaction Type', 'Product/Service', 'Qty', 'Size', 'Color', 'Amount',
        ])
        for tx in qs:
            writer.writerow([
                tx.display_id, tx.order_number,
                tx.order.store.name if tx.order_id and tx.order.store_id else '',
                _pc_time(tx), _user_time(tx),
                _tx_type_label(tx), tx.product_name, tx.quantity, tx.size, tx.color,
                str(_signed_amount(tx)),
            ])
    else:
        writer.writerow([
            'Transaction ID', 'User', 'PC Local Time', 'User Local Time', 'Shop', 'Order No',
            'Transaction Type', 'Item', 'Amount', 'Balance', 'Explanations',
        ])
        for tx, amt, running in _build_balance_history_rows(qs, balance_map or {}):
            writer.writerow([
                tx.display_id, _user_label(tx), _pc_time(tx), _user_time(tx),
                tx.order.store.name if tx.order_id and tx.order.store_id else '',
                tx.order_number, _tx_type_label(tx), _item_cell(tx),
                str(amt), str(running), tx.description,
            ])
    return buf.getvalue().encode('utf-8-sig')  # UTF-8 BOM for Excel compatibility


def _build_xlsx_bytes(qs, report, balance_map=None):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Item Report' if report == 'items' else 'Balance History'

    header_fill = PatternFill(start_color='222222', end_color='222222', fill_type='solid')
    header_font = Font(color='FFFFFF', bold=True)
    green_fill = PatternFill(start_color='E5FFE5', end_color='E5FFE5', fill_type='solid')
    white_fill = PatternFill(start_color='FFFFFF', end_color='FFFFFF', fill_type='solid')

    if report == 'items':
        headers = [
            'Transaction ID', 'Order No', 'Shop', 'PC Local Time', 'User Local Time',
            'Transaction Type', 'Product/Service', 'Qty', 'Size', 'Color', 'Amount',
        ]
    else:
        headers = [
            'Transaction ID', 'User', 'PC Local Time', 'User Local Time', 'Shop', 'Order No',
            'Transaction Type', 'Item', 'Amount', 'Balance', 'Explanations',
        ]

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')

    stripe_green = True
    last_group_key = object()  # sentinel so the very first row starts a fresh stripe
    row_idx = 2

    def _shop(tx):
        return tx.order.store.name if tx.order_id and tx.order.store_id else ''

    if report == 'items':
        rows_iter = ((tx, _signed_amount(tx), None) for tx in qs)
    else:
        rows_iter = _build_balance_history_rows(qs, balance_map or {})

    for tx, amt, running in rows_iter:
        if report == 'items':
            ws.append([
                tx.display_id, tx.order_number, _shop(tx), _pc_time(tx), _user_time(tx),
                _tx_type_label(tx), tx.product_name, tx.quantity, tx.size, tx.color, float(amt),
            ])
        else:
            ws.append([
                tx.display_id, _user_label(tx), _pc_time(tx), _user_time(tx), _shop(tx), tx.order_number,
                _tx_type_label(tx), _item_cell(tx), float(amt), float(running),
                tx.description,
            ])

        anomaly_color = _row_fill_color(tx)
        if anomaly_color:
            fill = PatternFill(start_color=anomaly_color, end_color=anomaly_color, fill_type='solid')
        else:
            # group_key: rows sharing a non-blank Order No form one stripe; a blank
            # Order No (manual adjustments) never groups with another row.
            group_key = tx.order_number or object()
            if group_key != last_group_key:
                stripe_green = not stripe_green
            last_group_key = group_key
            fill = green_fill if stripe_green else white_fill

        for col in range(1, len(headers) + 1):
            ws.cell(row=row_idx, column=col).fill = fill
        row_idx += 1

    for col in range(1, len(headers) + 1):
        max_len = max(
            len(str(ws.cell(row=r, column=col).value or ''))
            for r in range(1, ws.max_row + 1)
        )
        ws.column_dimensions[get_column_letter(col)].width = min(max_len + 4, 40)

    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


def _make_export_response(qs, fmt, filename_base, report, balance_map=None):
    """Build an HttpResponse for CSV or XLSX download. `report` is 'balance' or 'items'."""
    if fmt == 'xlsx':
        data = _build_xlsx_bytes(qs, report, balance_map)
        ct = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        fname = f'{filename_base}.xlsx'
    else:
        data = _build_csv_bytes(qs, report, balance_map)
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
        date_from, date_to = _parse_date_range(request)
        qs = BalanceTransaction.objects.filter(sub_user=sub)
        data = _compute_summary(sub, qs, date_from, date_to)
        return Response(BalanceSummarySerializer(data).data)


class MyTransactionListView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        sub = request.user.sub_user_profile
        date_from, date_to = _parse_date_range(request)
        tx_type = request.query_params.get('type')
        report = request.query_params.get('report', 'balance').lower()
        pc = sub.print_center

        # The "truth" scope (date/type/report — NOT the column filters below)
        # that Balance is computed against, so filtering down to e.g. one Shop
        # can't silently drop other rows' contribution to the running total.
        truth_qs = BalanceTransaction.objects.filter(sub_user=sub)
        truth_qs = _filter_transactions(truth_qs, date_from, date_to, tx_type, tz_name=pc.timezone)
        truth_qs = _apply_report_filter(truth_qs, report)

        balance_map = _compute_balance_map(truth_qs, pc, sub.pk, date_from) if report != 'items' else {}

        qs = _apply_column_filters(truth_qs, request, pc).select_related(
            'created_by', 'sub_user__user', 'sub_user__print_center', 'order', 'order__store'
        ).order_by('-created_at', '-id')

        page = int(request.query_params.get('page', 1))
        page_size = min(int(request.query_params.get('page_size', 50)), 200)
        total = qs.count()
        qs_page = list(qs[(page - 1) * page_size: page * page_size])

        for tx in qs_page:
            tx.balance_after = balance_map.get(tx.id, Decimal('0'))

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
        report = request.query_params.get('report', 'balance').lower()
        pc = sub.print_center
        suffix = f'{date_from or "all"}_{date_to or "all"}'

        truth_qs = BalanceTransaction.objects.filter(sub_user=sub)
        truth_qs = _filter_transactions(truth_qs, date_from, date_to, tz_name=pc.timezone)
        truth_qs = _apply_report_filter(truth_qs, report)

        balance_map = _compute_balance_map(truth_qs, pc, sub.pk, date_from) if report != 'items' else None

        qs = _apply_column_filters(truth_qs, request, pc).select_related(
            'created_by', 'sub_user__user', 'sub_user__print_center', 'order', 'order__store'
        ).order_by('created_at', 'id')

        name = 'item_report' if report == 'items' else 'balance_history'
        return _make_export_response(qs, fmt, f'{name}_{suffix}', report, balance_map)


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
        date_from, date_to = _parse_date_range(request)
        qs = BalanceTransaction.objects.filter(sub_user=sub)
        data = _compute_summary(sub, qs, date_from, date_to)
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
        report = request.query_params.get('report', 'balance').lower()
        pc = sub.print_center

        truth_qs = BalanceTransaction.objects.filter(sub_user=sub)
        truth_qs = _filter_transactions(truth_qs, date_from, date_to, tx_type, tz_name=pc.timezone)
        truth_qs = _apply_report_filter(truth_qs, report)

        balance_map = _compute_balance_map(truth_qs, pc, sub.pk, date_from) if report != 'items' else {}

        qs = _apply_column_filters(truth_qs, request, pc).select_related(
            'created_by', 'sub_user__user', 'sub_user__print_center', 'order', 'order__store'
        ).order_by('-created_at', '-id')

        page = int(request.query_params.get('page', 1))
        page_size = min(int(request.query_params.get('page_size', 50)), 200)
        total = qs.count()
        qs_page = list(qs[(page - 1) * page_size: page * page_size])

        for tx in qs_page:
            tx.balance_after = balance_map.get(tx.id, Decimal('0'))

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
        report = request.query_params.get('report', 'balance').lower()
        pc = sub.print_center
        suffix = f'{sub.user.email}_{date_from or "all"}_{date_to or "all"}'

        truth_qs = BalanceTransaction.objects.filter(sub_user=sub)
        truth_qs = _filter_transactions(truth_qs, date_from, date_to, tz_name=pc.timezone)
        truth_qs = _apply_report_filter(truth_qs, report)

        balance_map = _compute_balance_map(truth_qs, pc, sub.pk, date_from) if report != 'items' else None

        qs = _apply_column_filters(truth_qs, request, pc).select_related(
            'created_by', 'sub_user__user', 'sub_user__print_center', 'order', 'order__store'
        ).order_by('created_at', 'id')

        name = 'item_report' if report == 'items' else 'balance_history'
        return _make_export_response(qs, fmt, f'{name}_{suffix}', report, balance_map)


class PrintCenterTransactionListView(APIView):
    """Combined ledger across every sub-user under this PC — this IS the PC's
    own finance picture. `?sub_user=<id>` narrows to one sub-user, matching
    what that sub-user sees on their own Balance History page."""
    permission_classes = (IsPrintCenter,)

    def get(self, request):
        pc = request.user.print_center_profile
        date_from, date_to = _parse_date_range(request)
        tx_type = request.query_params.get('type')
        sub_user_id = request.query_params.get('sub_user')
        report = request.query_params.get('report', 'balance').lower()

        if sub_user_id and not SubUserProfile.objects.filter(pk=sub_user_id, print_center=pc).exists():
            raise NotFound()

        # The "truth" scope (date/sub-user/type/report — NOT the column
        # filters below) that Balance is computed against, so narrowing the
        # displayed rows (e.g. to one Shop) can't silently drop other rows'
        # contribution to the running total. A single sub-user: their own
        # running Balance (identical to what they see on their own page).
        # All sub-users combined: ONE running total across everyone — this IS
        # the PC's own net position, not several balances stacked confusingly
        # in one column.
        truth_qs = BalanceTransaction.objects.filter(sub_user__print_center=pc)
        if sub_user_id:
            truth_qs = truth_qs.filter(sub_user_id=sub_user_id)
        truth_qs = _filter_transactions(truth_qs, date_from, date_to, tx_type, tz_name=pc.timezone)
        truth_qs = _apply_report_filter(truth_qs, report)

        balance_map = _compute_balance_map(truth_qs, pc, sub_user_id, date_from) if report != 'items' else {}

        qs = _apply_column_filters(truth_qs, request, pc).select_related(
            'created_by', 'sub_user__user', 'sub_user__print_center', 'order', 'order__store'
        ).order_by('-created_at', '-id')

        page = int(request.query_params.get('page', 1))
        page_size = min(int(request.query_params.get('page_size', 50)), 200)
        total = qs.count()
        qs_page = list(qs[(page - 1) * page_size: page * page_size])

        for tx in qs_page:
            tx.balance_after = balance_map.get(tx.id, Decimal('0'))

        return Response({
            'count': total,
            'page': page,
            'page_size': page_size,
            'results': BalanceTransactionSerializer(qs_page, many=True).data,
        })


class PrintCenterTransactionExportView(_ExportMixin, APIView):
    permission_classes = (IsPrintCenter,)

    def get(self, request):
        pc = request.user.print_center_profile
        date_from, date_to = _parse_date_range(request)
        fmt = request.query_params.get('format', 'csv').lower()
        report = request.query_params.get('report', 'balance').lower()
        sub_user_id = request.query_params.get('sub_user')

        if sub_user_id and not SubUserProfile.objects.filter(pk=sub_user_id, print_center=pc).exists():
            raise NotFound()

        suffix = f'{date_from or "all"}_{date_to or "all"}'

        truth_qs = BalanceTransaction.objects.filter(sub_user__print_center=pc)
        if sub_user_id:
            truth_qs = truth_qs.filter(sub_user_id=sub_user_id)
        truth_qs = _filter_transactions(truth_qs, date_from, date_to, tz_name=pc.timezone)
        truth_qs = _apply_report_filter(truth_qs, report)

        balance_map = _compute_balance_map(truth_qs, pc, sub_user_id, date_from) if report != 'items' else None

        qs = _apply_column_filters(truth_qs, request, pc).select_related(
            'created_by', 'sub_user__user', 'sub_user__print_center', 'order', 'order__store'
        ).order_by('created_at', 'id')

        name = 'item_report' if report == 'items' else 'balance_history'
        return _make_export_response(qs, fmt, f'{name}_{suffix}', report, balance_map)


def _build_filter_options(qs, pc):
    """Distinct values for the Excel-style column filter dropdowns on a
    Balance History table — `qs` is whatever base queryset (one sub-user's
    own, or a PC's combined one) the caller has already scoped/date-filtered;
    `pc` supplies the timezone for the PC Local Time date grouping."""
    shops = list(
        qs.exclude(order__store__isnull=True)
        .values_list('order__store__name', flat=True)
        .distinct().order_by('order__store__name')
    )

    cat_labels = dict(BalanceTransaction.Category.choices)
    type_labels = dict(BalanceTransaction.Type.choices)
    cats_present = qs.exclude(category='').values_list('category', flat=True).distinct()
    types_present = qs.values_list('type', flat=True).distinct()
    types = (
        [{'code': f'cat:{c}', 'label': cat_labels.get(c, c)} for c in cats_present] +
        [{'code': f'type:{t}', 'label': type_labels.get(t, t)} for t in types_present]
    )

    item_rows = (
        qs.exclude(product_name='').values('product_name', 'size', 'color')
        .distinct().order_by('product_name', 'size', 'color')
    )
    items = [
        {
            'value': {'product_name': r['product_name'], 'size': r['size'], 'color': r['color']},
            'label': ' - '.join(p for p in (r['product_name'], r['size'], r['color']) if p),
        }
        for r in item_rows
    ]

    explanations = list(
        qs.exclude(description='').values_list('description', flat=True)
        .distinct().order_by('description')
    )

    with timezone.override(ZoneInfo(pc.timezone)):
        pc_dates = sorted({
            d.isoformat() for d in
            qs.annotate(_d=TruncDate('created_at')).values_list('_d', flat=True).distinct()
        })

    user_dates = sorted({
        tx.created_at.astimezone(ZoneInfo(tx.sub_user.timezone)).date().isoformat()
        for tx in qs.select_related('sub_user').only('id', 'created_at', 'sub_user__timezone')
    })

    return {
        'shops': shops,
        'types': types,
        'items': items,
        'explanations': explanations,
        'pc_dates': pc_dates,
        'user_dates': user_dates,
    }


class MyTransactionFilterOptionsView(APIView):
    """Distinct values for the Excel-style column filter dropdowns on a
    sub-user's own Balance History table."""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        sub = request.user.sub_user_profile
        date_from, date_to = _parse_date_range(request)
        qs = _filter_transactions(
            BalanceTransaction.objects.filter(sub_user=sub), date_from, date_to,
            tz_name=sub.print_center.timezone,
        )
        return Response(_build_filter_options(qs, sub.print_center))


class PrintCenterTransactionFilterOptionsView(APIView):
    """Distinct values for the Excel-style column filter dropdowns on the PC's
    combined Balance History table — scoped to whatever date range / sub-user
    is already selected, like Excel narrows its own column filter list."""
    permission_classes = (IsPrintCenter,)

    def get(self, request):
        pc = request.user.print_center_profile
        date_from, date_to = _parse_date_range(request)
        sub_user_id = request.query_params.get('sub_user')

        if sub_user_id and not SubUserProfile.objects.filter(pk=sub_user_id, print_center=pc).exists():
            raise NotFound()

        qs = BalanceTransaction.objects.filter(sub_user__print_center=pc)
        if sub_user_id:
            qs = qs.filter(sub_user_id=sub_user_id)
        qs = _filter_transactions(qs, date_from, date_to, tz_name=pc.timezone)

        return Response(_build_filter_options(qs, pc))


class PrintCenterFinanceSummaryView(APIView):
    """Aggregate tiles for the PC's own Finance page — there's no single
    'current balance' the way a sub-user has one, so `current_balance` here
    is repurposed as the net combined balance across the selected sub-user(s)
    (see `_balance_seed_combined` / PrintCenterTransactionListView)."""
    permission_classes = (IsPrintCenter,)

    def get(self, request):
        pc = request.user.print_center_profile
        sub_user_id = request.query_params.get('sub_user')
        date_from, date_to = _parse_date_range(request)

        if sub_user_id and not SubUserProfile.objects.filter(pk=sub_user_id, print_center=pc).exists():
            raise NotFound()

        qs = BalanceTransaction.objects.filter(sub_user__print_center=pc)
        if sub_user_id:
            qs = qs.filter(sub_user_id=sub_user_id)

        # `total_*` and `this_month_*` both scope to the caller's selected
        # period (same Month/Custom Range filter as the table below) rather
        # than a fixed real-world 'this calendar month' — see _compute_summary.
        qs_period = _filter_transactions(qs, date_from, date_to, tz_name=pc.timezone)

        def agg(queryset, type_):
            return queryset.filter(type=type_).aggregate(s=Sum('amount'))['s'] or Decimal('0')

        # Net combined balance across the selected scope — same sign convention
        # as a sub-user's own "Current Balance" (negative = they owe money,
        # positive = they're in credit), just summed. This is the exact same
        # figure the Balance column's last (most recent) row converges to when
        # "All Sub-Users" is selected with no date filter — see
        # PrintCenterTransactionListView / _balance_seed_combined.
        sub_qs = SubUserProfile.objects.filter(print_center=pc)
        if sub_user_id:
            sub_qs = sub_qs.filter(pk=sub_user_id)
        combined_balance = sub_qs.aggregate(s=Sum('balance'))['s'] or Decimal('0')

        # Badge on the Invoices tab is about what THIS PC needs to act on — its
        # sub-users' unpaid invoices — not its own upward bill from Super Admin.
        unpaid_qs = SubUserInvoice.objects.filter(
            sub_user__print_center=pc, status__in=('pending', 'overdue'),
        )
        if sub_user_id:
            unpaid_qs = unpaid_qs.filter(sub_user_id=sub_user_id)

        data = {
            'current_balance': combined_balance,
            'total_credited': agg(qs_period, BalanceTransaction.Type.CREDIT),
            'total_debited': agg(qs_period, BalanceTransaction.Type.DEBIT),
            'total_refunded': agg(qs_period, BalanceTransaction.Type.REFUND),
            'this_month_credited': agg(qs_period, BalanceTransaction.Type.CREDIT),
            'this_month_debited': agg(qs_period, BalanceTransaction.Type.DEBIT),
            'transaction_count': qs_period.count(),
            'unpaid_invoice_count': unpaid_qs.count(),
        }
        return Response(BalanceSummarySerializer(data).data)


# ── Sub-user invoices (monthly, generated by generate_monthly_invoices) ───────

def _invoice_filter(qs, request):
    year = request.query_params.get('year')
    month = request.query_params.get('month')
    if year:
        qs = qs.filter(period_year=year)
    if month:
        qs = qs.filter(period_month=month)
    return qs


class MyInvoiceListView(generics.ListAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = SubUserInvoiceSerializer

    def get_queryset(self):
        qs = SubUserInvoice.objects.filter(sub_user=self.request.user.sub_user_profile)
        return _invoice_filter(qs, self.request)


class SubUserInvoiceListView(generics.ListAPIView):
    permission_classes = (IsPrintCenter,)
    serializer_class = SubUserInvoiceSerializer

    def get_queryset(self):
        try:
            sub = SubUserProfile.objects.get(
                pk=self.kwargs['pk'], print_center=self.request.user.print_center_profile
            )
        except SubUserProfile.DoesNotExist:
            raise NotFound()
        qs = SubUserInvoice.objects.filter(sub_user=sub)
        return _invoice_filter(qs, self.request)


class PrintCenterSubUserInvoiceListView(generics.ListAPIView):
    """Combined list of every sub-user's invoices under this PC — this is
    where the PC actually manages what it's owed (mark paid, add a note),
    via the existing per-sub-user SubUserInvoiceDetailView PATCH below.
    Optional filters: ?sub_user=, ?status=, ?year=, ?month=."""
    permission_classes = (IsPrintCenter,)
    serializer_class = SubUserInvoiceSerializer

    def get_queryset(self):
        pc = self.request.user.print_center_profile
        qs = SubUserInvoice.objects.filter(sub_user__print_center=pc).select_related('sub_user__user')

        sub_user_id = self.request.query_params.get('sub_user')
        status_param = self.request.query_params.get('status')
        if sub_user_id:
            qs = qs.filter(sub_user_id=sub_user_id)
        if status_param:
            qs = qs.filter(status=status_param)
        return _invoice_filter(qs, self.request).order_by('-period_year', '-period_month', 'sub_user__user__email')


class SubUserInvoiceDetailView(APIView):
    """PC marks one of its sub-user's invoices paid (or adds a note) — same
    whitelist-patch pattern as the superadmin-facing PrintCenterInvoice view."""
    permission_classes = (IsPrintCenter,)

    def _get_invoice(self, request, pk, invoice_pk):
        try:
            return SubUserInvoice.objects.select_related('sub_user__user').get(
                pk=invoice_pk, sub_user_id=pk, sub_user__print_center=request.user.print_center_profile,
            )
        except SubUserInvoice.DoesNotExist:
            raise NotFound()

    @transaction.atomic
    def patch(self, request, pk, invoice_pk):
        invoice = self._get_invoice(request, pk, invoice_pk)
        allowed = {k: v for k, v in request.data.items() if k in ('status', 'note')}
        new_status = allowed.get('status')

        if new_status == 'paid' and not invoice.paid_at:
            allowed['paid_at'] = timezone.now().isoformat()
            # Credit the sub-user's balance back by the invoice amount — this is
            # what actually settles the debt the monthly cycle accrued, same as
            # every other BalanceTransaction creation site keeps `balance` and the
            # ledger in lockstep (send_to_print, AddBalanceView, refund_order_debit).
            sub_locked = SubUserProfile.objects.select_for_update().get(pk=invoice.sub_user_id)
            sub_locked.balance += invoice.amount
            sub_locked.save(update_fields=['balance'])
            BalanceTransaction.objects.create(
                sub_user=sub_locked,
                type=BalanceTransaction.Type.CREDIT,
                category=BalanceTransaction.Category.INVOICE_PAYMENT,
                amount=invoice.amount,
                description=f'Invoice {invoice.period_year}/{invoice.period_month:02d} paid',
                created_by=request.user,
            )
        elif new_status and new_status != 'paid' and invoice.paid_at:
            # Reverting a previously-paid invoice (e.g. marked paid by mistake) —
            # reverse exactly what marking it paid did. The original payment
            # entry is never deleted (audit trail); an offsetting debit is added
            # instead, same convention as every other reversal in this ledger.
            sub_locked = SubUserProfile.objects.select_for_update().get(pk=invoice.sub_user_id)
            sub_locked.balance -= invoice.amount
            sub_locked.save(update_fields=['balance'])
            BalanceTransaction.objects.create(
                sub_user=sub_locked,
                type=BalanceTransaction.Type.DEBIT,
                category=BalanceTransaction.Category.INVOICE_PAYMENT,
                amount=invoice.amount,
                description=f'Invoice {invoice.period_year}/{invoice.period_month:02d} payment reverted',
                created_by=request.user,
            )
            allowed['paid_at'] = None
            # Land back on 'overdue' instead of 'pending' if it's already past
            # due — matches what mark_overdue_invoices would have set had this
            # invoice never been paid.
            if new_status == 'pending' and invoice.due_date < timezone.now().date():
                allowed['status'] = 'overdue'

        for k, v in allowed.items():
            setattr(invoice, k, v)
        invoice.save()
        return Response(SubUserInvoiceSerializer(invoice).data)
