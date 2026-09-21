"""Production batch workflow.

    preflight  ->  label pass  ->  layout (sheets + slips)  ->  ready

* preflight   free checks, run before anything is bought.
* label pass  one order at a time; an order that fails never blocks or invalidates the
              others. Every order carries its own state (BatchOrder.label_status).
* layout      runs only once no order is still unresolved, and only with LABELED orders:
              an order without a label is never printed. Because labels are settled first,
              sheet numbering and packing are computed once over the final set.
"""
from __future__ import annotations

import io
import logging
import re
import threading
from typing import Dict, List

from django.core.files.base import ContentFile
from django.db import close_old_connections
from django.db.models import Q

from .models import BatchOrder, ProductionBatch

logger = logging.getLogger(__name__)

# Batches whose orders are still on their way to production — an order in one of
# these must not be picked up by a second batch.
OPEN_BATCH_STATUSES = (
    ProductionBatch.Status.PENDING,
    ProductionBatch.Status.LABELING,
    ProductionBatch.Status.NEEDS_ATTENTION,
    ProductionBatch.Status.PROCESSING,
)
# Stop the label pass when this many orders in a row fail with the same
# account-level problem (e.g. provider balance): the rest would fail identically.
ACCOUNT_ERROR_STREAK = 3


def run_in_background(task, impl, *args):
    """Queue on Celery; if no broker is reachable (local development) run in a thread."""
    try:
        task.delay(*args)
    except Exception:
        logger.warning('Celery unavailable — running %s in a thread', impl.__name__)

        def _run():
            close_old_connections()
            try:
                impl(*args)
            finally:
                close_old_connections()

        threading.Thread(target=_run, daemon=True).start()


# ── Preflight ────────────────────────────────────────────────────────────────

_ZIP_RE = re.compile(r'^\d{5}(-\d{4})?$')


def orders_in_open_batches(order_ids) -> set:
    return set(
        BatchOrder.objects.filter(order_id__in=order_ids, batch__status__in=OPEN_BATCH_STATUSES)
        .exclude(label_status=BatchOrder.LabelStatus.REMOVED)
        .values_list('order_id', flat=True)
    )


def design_size_inches(item, design):
    """(width, height) in inches at which this design is printed; ValueError if the
    catalog does not say how big it should be.

    The garment size decides it: PrintAreaSizeSpec gives the maximum print dimension
    for a print area + size (an XL front prints bigger than an S front). The design's
    own proportions are always kept — it is scaled, never stretched. Without a size
    spec the print area's own width/height is used as a bounding box. A missing or
    zero size is an error, never a guess: the result is what ends up on film.
    """
    variant = item.pc_variant
    area = None
    if variant is not None and variant.pc_product_id:
        area = variant.pc_product.print_areas.filter(name=design.print_area_name).first()
    if not area:
        raise ValueError(f'Print area "{design.print_area_name}" does not exist on the product in the catalog.')

    px_w, px_h = design.width_px or 0, design.height_px or 0
    if px_w <= 0 or px_h <= 0:
        from PIL import Image
        with design.file.open('rb') as fh, Image.open(fh) as im:
            px_w, px_h = im.size
    ratio = px_w / px_h

    spec = area.size_specs.filter(size__iexact=variant.size or '').first()
    if spec is not None and float(spec.max_dim_in) > 0:
        long_side = float(spec.max_dim_in)
        return (long_side, long_side / ratio) if ratio >= 1 else (long_side * ratio, long_side)
    if float(area.width_inches) > 0 and float(area.height_inches) > 0:
        scale = min(float(area.width_inches) / px_w, float(area.height_inches) / px_h)
        return px_w * scale, px_h * scale
    raise ValueError(
        f'Print area "{design.print_area_name}" has no size for {variant.size or "this size"} in the '
        'catalog — fill it in the print-area size grid.'
    )


def _design_problems(order, sheet_width_in, margin_in):
    """Every physical item needs a readable design that fits the sheet width."""
    from PIL import Image

    problems = []
    for item in order.items.select_related('pc_variant__pc_product').prefetch_related('designs'):
        if item.unit_price < 0:
            continue
        designs = list(item.designs.all())
        if not designs:
            problems.append(('error', 'design_missing', f'"{item.raw_product_name[:40]}" has no design.'))
            continue
        for d in designs:
            try:
                with d.file.open('rb') as fh, Image.open(fh) as im:
                    im.verify()
            except Exception:
                problems.append(('error', 'design_unreadable', f'Design file "{d.file_name}" cannot be read.'))
                continue
            try:
                width, _height = design_size_inches(item, d)
            except ValueError as exc:
                problems.append(('error', 'print_area_no_size', str(exc)))
                continue
            if width > sheet_width_in - 2 * margin_in:
                problems.append((
                    'error', 'design_too_wide',
                    f'"{d.print_area_name}" prints {width:g}" wide — wider than the sheet allows.',
                ))
    return problems


def preflight(orders, sheet_width_in=22.0, margin_in=0.2) -> Dict:
    """Free checks for a set of orders, before anything is bought.

    Returns {'orders': [...only orders with findings...], 'ok_ids': [...], 'blocked_ids': [...],
    'batch': [...findings about the whole batch...]}. severity 'error' blocks an order
    from the batch; 'warning' is shown but allowed.
    """
    from apps.orders.models import Order
    from apps.shipping.models import LabelProviderAccount, ShippingServiceOption
    from apps.shipping.utils import categorize_requested_service, get_order_shipping_config, sanitize_label_text

    orders = list(orders)
    open_ids = orders_in_open_batches([o.id for o in orders])
    result_orders, ok_ids, blocked_ids = [], [], []
    pc_cache: dict = {}

    for order in orders:
        found = []
        pc = order.store.shipstation_account.print_center
        if pc.pk not in pc_cache:
            accounts = set(LabelProviderAccount.objects.filter(print_center=pc, is_active=True).values_list('provider', flat=True))
            options = list(ShippingServiceOption.objects.filter(print_center=pc, is_active=True))
            pc_cache[pc.pk] = (accounts, options)
        accounts, options = pc_cache[pc.pk]

        if order.id in open_ids:
            found.append(('error', 'in_other_batch', 'Already part of another batch that is still in progress.'))
        if order.status != Order.Status.SENT_TO_PRINT:
            found.append(('error', 'wrong_status', f'Status is "{order.get_status_display()}", not Sent to Print.'))

        if not order.store.ship_from_configured:
            found.append((
                'error', 'no_ship_from',
                f'Store "{order.store.name}" has no ship-from address — sync it on the ShipStation page.',
            ))

        missing = [
            label for field, label in (
                ('ship_to_name', 'name'), ('ship_to_address1', 'street'), ('ship_to_city', 'city'),
                ('ship_to_state', 'state'), ('ship_to_postal_code', 'ZIP'),
            ) if not sanitize_label_text(getattr(order, field))
        ]
        if missing:
            found.append((
                'error', 'address_incomplete',
                'Delivery address is missing or unusable (' + ', '.join(missing) + ') — fix the address.',
            ))
        elif (order.ship_to_country or 'US').upper() == 'US' and not _ZIP_RE.match(order.ship_to_postal_code.strip()):
            found.append(('error', 'bad_zip', f'ZIP "{order.ship_to_postal_code}" is not a valid US ZIP code.'))

        if order.address_status == Order.AddressStatus.FAILED:
            found.append(('error', 'address_failed', 'ShipStation could not validate the delivery address.'))
        elif order.address_status == Order.AddressStatus.WARNING:
            found.append(('warning', 'address_warning', 'ShipStation flagged the address (e.g. apartment/suite) — worth a look.'))

        weight_oz, _ = get_order_shipping_config(order)
        if not weight_oz:
            found.append(('error', 'no_weight', 'Order weight is unknown — match its products or set a weight override.'))

        if not order.selected_carrier_code or order.shipping_cost is None:
            found.append(('error', 'no_shipping_selected', 'No shipping method/price was selected for this order.'))

        category = categorize_requested_service(order.requested_service)
        if not any(o.category == category and o.provider in accounts for o in options):
            found.append((
                'error', 'no_shipping_option',
                f'No active provider has a "{category}" shipping option configured (Shipping Settings).',
            ))

        found.extend(_design_problems(order, sheet_width_in, margin_in))

        if any(sev == 'error' for sev, _, _ in found):
            blocked_ids.append(order.id)
        else:
            ok_ids.append(order.id)
        if found:
            result_orders.append({
                'order_id': order.id,
                'order_number': order.shipstation_order_number or order.external_order_number,
                'customer': order.customer_name,
                'store': order.store.name,
                'problems': [{'severity': s, 'code': c, 'message': m} for s, c, m in found],
            })

    return {
        'orders': result_orders, 'ok_ids': ok_ids, 'blocked_ids': blocked_ids,
        'batch': _provider_notes(orders, ok_ids),
    }


def _provider_notes(orders, ok_ids) -> List[Dict]:
    """Batch-level findings: is the ByeLabel balance enough for what's about to be bought?"""
    from apps.shipping.byelabel_client import ByeLabelClient
    from apps.shipping.models import LabelProviderAccount

    notes = []
    if not orders:
        return notes
    pcs = {o.store.shipstation_account.print_center_id for o in orders}
    estimate = sum(float(o.shipping_cost_raw or 0) for o in orders if o.id in set(ok_ids))
    for account in LabelProviderAccount.objects.filter(
        print_center_id__in=pcs, is_active=True, provider=LabelProviderAccount.Provider.BYELABEL,
    ):
        try:
            balance = next((float(b['amount']) for b in ByeLabelClient(account).get_balance() if b.get('currency') == 'USD'), None)
        except Exception as exc:
            notes.append({'severity': 'warning', 'code': 'provider_unreachable',
                          'message': f'Could not check the "{account.label}" balance: {exc}'[:200]})
            continue
        if balance is not None and balance < estimate:
            notes.append({
                'severity': 'warning', 'code': 'balance_low',
                'message': f'"{account.label}" balance is ${balance:,.2f}; these labels may cost about ${estimate:,.2f}. '
                           'Top it up first or the label pass will stop.',
            })
    return notes


# ── Label pass ───────────────────────────────────────────────────────────────

def _apply_outcome(bo: BatchOrder, outcome):
    if outcome.ok:
        bo.label_status = BatchOrder.LabelStatus.LABELED
        bo.error_kind, bo.error_message = '', ''
        bo.label_purchased = True
    elif outcome.status == 'needs_review':
        bo.label_status = BatchOrder.LabelStatus.NEEDS_REVIEW
        bo.error_kind = BatchOrder.ErrorKind.AMBIGUOUS
        bo.error_message = outcome.message[:1000]
    else:
        bo.label_status = BatchOrder.LabelStatus.FAILED
        bo.error_kind = outcome.kind or BatchOrder.ErrorKind.OTHER
        bo.error_message = outcome.message[:1000]
    bo.save(update_fields=['label_status', 'error_kind', 'error_message', 'label_purchased'])


def _claim(batch_id, new_status, blocked_statuses):
    """Atomically move a batch into a running status; False if it already is running."""
    return ProductionBatch.objects.filter(pk=batch_id).exclude(status__in=blocked_statuses).update(status=new_status) == 1


def run_label_pass(batch_id: int):
    """Buy a label for every waiting order in the batch, one by one, then decide
    what happens next (see advance)."""
    from apps.orders.models import Order
    from apps.shipping.client import reset_throttle, set_throttle
    from apps.shipping.services import PurchaseOutcome, purchase_label_for_order

    if not _claim(batch_id, ProductionBatch.Status.LABELING, (ProductionBatch.Status.LABELING, ProductionBatch.Status.PROCESSING)):
        return
    ProductionBatch.objects.filter(pk=batch_id).update(paused_reason='', error_message='')

    token = set_throttle(True)
    streak_message, streak = '', 0
    try:
        waiting = BatchOrder.objects.filter(batch_id=batch_id, label_status=BatchOrder.LabelStatus.PENDING).order_by('id')
        for bo in waiting:
            bo.refresh_from_db(fields=['label_status'])
            if bo.label_status != BatchOrder.LabelStatus.PENDING:
                continue
            order = (
                Order.objects.select_related('store__shipstation_account__print_center', 'sub_user')
                .prefetch_related('items__pc_variant__pc_product', 'items__pc_variant__dimension_rules', 'items__designs')
                .get(pk=bo.order_id)
            )
            try:
                outcome = purchase_label_for_order(order, batch_order=bo)
            except Exception as exc:  # never let one order kill the whole pass
                logger.exception('Unexpected error labeling order %s', bo.order_id)
                outcome = PurchaseOutcome('failed', 'other', f'Unexpected error: {exc}')
            _apply_outcome(bo, outcome)

            if outcome.kind == 'account':
                streak = streak + 1 if outcome.message == streak_message else 1
                streak_message = outcome.message
                if streak >= ACCOUNT_ERROR_STREAK:
                    ProductionBatch.objects.filter(pk=batch_id).update(paused_reason=(
                        f'Stopped after {streak} orders in a row failed with the same provider problem: '
                        f'{outcome.message[:300]} — fix it, then choose "Retry failed".'
                    ))
                    break
            else:
                streak_message, streak = '', 0
    finally:
        reset_throttle(token)
    advance(batch_id)


def advance(batch_id: int):
    """After any change to a batch's orders decide what comes next: build the sheets
    if every order is settled, otherwise wait for the print center."""
    batch = ProductionBatch.objects.get(pk=batch_id)
    if batch.status in (ProductionBatch.Status.PROCESSING, ProductionBatch.Status.READY, ProductionBatch.Status.COMPLETED):
        return
    states = BatchOrder.objects.filter(batch=batch).exclude(label_status=BatchOrder.LabelStatus.REMOVED)
    unresolved = states.exclude(label_status=BatchOrder.LabelStatus.LABELED).exists()
    labeled = states.filter(label_status=BatchOrder.LabelStatus.LABELED).exists()

    if unresolved or batch.paused_reason:
        batch.status = ProductionBatch.Status.NEEDS_ATTENTION
        batch.save(update_fields=['status'])
    elif labeled:
        from .tasks import layout_batch
        run_in_background(layout_batch, run_layout, batch_id)
    else:
        batch.status = ProductionBatch.Status.FAILED
        batch.error_message = 'No orders left in this batch.'
        batch.save(update_fields=['status', 'error_message'])


# ── Layout ───────────────────────────────────────────────────────────────────

class _LazyDesignFiles:
    """design_id -> file bytes on demand, so a large batch never holds thousands of
    file handles open at once."""

    def __init__(self, designs):
        self._designs = designs

    def get(self, design_id):
        design = self._designs.get(design_id)
        if not design or not design.file:
            return None
        try:
            with design.file.open('rb') as fh:
                return io.BytesIO(fh.read())
        except Exception:
            return None


def run_layout(batch_id: int):
    """Pack the labeled orders' designs onto gang sheets, render PNGs, build the slip
    PDFs (sheet preview + one page per order) and move the orders to In Production.
    Costs nothing and can be re-run: previous sheets/slips of this batch are replaced."""
    from apps.orders.models import Order
    from apps.orders.services import log_event

    from .gang_sheet import render_gang_sheet, render_sheet_preview
    from .models import GangSheet, OrderSlipPDF
    from .packing import pack, total_length
    from .slip_pdf import generate_slip_pdf

    if not _claim(batch_id, ProductionBatch.Status.PROCESSING, (ProductionBatch.Status.PROCESSING,)):
        return
    batch = ProductionBatch.objects.get(pk=batch_id)

    try:
        batch_orders = list(
            BatchOrder.objects.filter(batch=batch, label_status=BatchOrder.LabelStatus.LABELED)
            .select_related('order__store')
            .prefetch_related(
                'order__items__designs', 'order__items__pc_variant__pc_product__print_areas__size_specs',
            ).order_by('order__ordered_at', 'order_id')
        )
        if not batch_orders:
            raise RuntimeError('No labeled orders to build sheets for.')

        # Replace anything a previous run produced.
        for slip in batch.slip_pdfs.all():
            slip.file.delete(save=False)
        for gs in batch.gang_sheets.all():
            gs.file.delete(save=False)
        batch.slip_pdfs.all().delete()
        batch.gang_sheets.all().delete()

        margin = float(batch.margin_inches)
        dpi = batch.gang_sheet_dpi
        max_len = float(batch.sheet_length_inches)

        designs, rects = {}, []
        for bo in batch_orders:
            for item in bo.order.items.all():
                if item.unit_price < 0:
                    continue
                for design in item.designs.all():
                    try:
                        width_in, height_in = design_size_inches(item, design)
                    except ValueError as exc:
                        raise RuntimeError(f'Order {bo.order.shipstation_order_number}: {exc}') from exc
                    rects.append((design.id, bo.order_id, width_in, height_in))
                    designs[design.id] = design
        if not rects:
            raise RuntimeError('The labeled orders have no designs to print.')

        sheets = pack(rects, sheet_width=22.0, margin=margin, max_sheet_length=max_len)
        files = _LazyDesignFiles(designs)
        order_by_id = {bo.order_id: bo.order for bo in batch_orders}
        bo_by_order = {bo.order_id: bo for bo in batch_orders}

        for number, placements in enumerate(sheets, start=1):
            length = round(min(total_length(placements, margin) + margin, max_len), 2)
            png = render_gang_sheet(placements, files, sheet_length_in=length, dpi=dpi, margin_in=margin)
            sheet = GangSheet(batch=batch, sheet_number=number, width_inches=22, length_inches=length)
            sheet.file.save(f'batch_{batch.id}_sheet_{number}.png', ContentFile(png), save=True)

            first_seen: dict = {}
            for p in placements:
                first_seen.setdefault(p.order_id, p.group_seq)
            ordered_ids = sorted(first_seen, key=lambda oid: first_seen[oid])
            for oid in ordered_ids:
                bo = bo_by_order[oid]
                bo.sequence_number = first_seen[oid]
                bo.save(update_fields=['sequence_number'])

            labels = {oid: (order_by_id[oid].shipstation_order_number or order_by_id[oid].external_order_number)
                      for oid in ordered_ids}
            preview = render_sheet_preview(placements, files, labels, length)
            pdf = generate_slip_pdf([order_by_id[oid] for oid in ordered_ids], number, preview=preview)
            slip = OrderSlipPDF(batch=batch, gang_sheet=sheet)
            slip.file.save(f'batch_{batch.id}_slip_{number}.pdf', ContentFile(pdf), save=True)

        ids = [bo.order_id for bo in batch_orders]
        Order.objects.filter(pk__in=ids).update(status=Order.Status.IN_PRODUCTION)
        for order in order_by_id.values():
            log_event(order, 'in_production', description=f'Batch #{batch.id}')

        batch.status = ProductionBatch.Status.READY
        batch.error_message = ''
        batch.save(update_fields=['status', 'error_message'])
    except Exception as exc:
        logger.exception('Layout failed for batch %s', batch_id)
        ProductionBatch.objects.filter(pk=batch_id).update(
            status=ProductionBatch.Status.FAILED, error_message=str(exc)[:1000],
        )
