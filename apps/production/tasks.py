"""Celery tasks for production batch processing."""
import io
import logging

from celery import shared_task
from django.core.files.base import ContentFile
from django.db import transaction

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def generate_batch(self, batch_id: int):
    """
    Full production batch pipeline:
    1. Collect designs for all orders in the batch
    2. Run bin packing
    3. Render gang sheet PNGs
    4. Generate order slip PDFs (labels as placeholders)
    5. Purchase ShipStation labels
    6. Regenerate slip PDFs with actual labels
    7. Mark batch as READY
    """
    from .models import BatchOrder, GangSheet, OrderSlipPDF, ProductionBatch

    try:
        batch = ProductionBatch.objects.select_related('print_center').get(pk=batch_id)
    except ProductionBatch.DoesNotExist:
        return

    batch.status = ProductionBatch.Status.PROCESSING
    batch.save(update_fields=['status'])

    try:
        _run_batch(batch)
        batch.status = ProductionBatch.Status.READY
        batch.save(update_fields=['status'])
    except Exception as exc:
        logger.exception('Batch %s failed: %s', batch_id, exc)
        batch.status = ProductionBatch.Status.FAILED
        batch.error_message = str(exc)
        batch.save(update_fields=['status', 'error_message'])
        raise self.retry(exc=exc)


def _run_batch(batch):
    from apps.designs.models import OrderItemDesign
    from apps.orders.models import Order
    from apps.orders.services import log_event

    from .gang_sheet import render_gang_sheet
    from .models import BatchOrder, GangSheet, OrderSlipPDF
    from .packing import pack, total_length
    from .slip_pdf import generate_slip_pdf

    batch_orders = (
        BatchOrder.objects.filter(batch=batch)
        .select_related('order__store__shipstation_account',
                        'order__sub_user')
        .prefetch_related('order__items__designs',
                          'order__items__pc_variant__pc_product__print_areas',
                          'order__items__pc_variant__dimension_rules')
        .order_by('sequence_number')
    )

    margin = float(batch.margin_inches)
    dpi = batch.gang_sheet_dpi
    max_len = float(batch.sheet_length_inches)

    # ── Collect designs ──────────────────────────────────────────────────────
    design_rects = []   # (design_id, order_id, w_in, h_in)
    design_file_map = {}  # design_id → file obj

    for bo in batch_orders:
        order = bo.order
        for item in order.items.all():
            for design in item.designs.all():
                # Get physical print area dimensions
                area = None
                if item.pc_variant:
                    area = item.pc_variant.pc_product.print_areas.filter(
                        name=design.print_area_name
                    ).first()
                w_in = float(area.width_inches) if area else 12.0
                h_in = float(area.height_inches) if area else 14.0

                design_rects.append((design.id, order.id, w_in, h_in))

                if design.file:
                    try:
                        design_file_map[design.id] = design.file.open('rb')
                    except Exception:
                        design_file_map[design.id] = None

    if not design_rects:
        return

    # ── Pack designs onto sheets ─────────────────────────────────────────────
    sheets = pack(design_rects, sheet_width=22.0, margin=margin, max_sheet_length=max_len)

    # Build seq mapping: design_id → group_seq (for slip pairing)
    design_seq_map = {}
    for sheet_placements in sheets:
        for p in sheet_placements:
            design_seq_map[p.design_id] = p.group_seq

    # Build order → sheet index mapping
    order_sheet_map = {}
    for sheet_idx, sheet_placements in enumerate(sheets):
        for p in sheet_placements:
            order_sheet_map[p.order_id] = sheet_idx

    # ── Purchase labels (rate-shopped across every active provider) ──────────
    #
    # purchase_label_for_order() is idempotent: if this task is retried
    # (Celery retries the whole batch on any exception in _run_batch — e.g. a
    # later order's gang-sheet rendering failing), an order whose label was
    # already bought on a prior attempt is never charged/purchased again.
    # See apps/shipping/services.py for the locking + audit-trail details.
    from apps.shipping.services import purchase_label_for_order

    label_images = {}
    for bo in batch_orders:
        order = bo.order
        if not order.selected_carrier_code:
            continue
        try:
            image_bytes = purchase_label_for_order(order, batch_order=bo)
            if image_bytes:
                label_images[order.id] = image_bytes
        except Exception as exc:
            logger.exception('Unexpected error purchasing label for order %s: %s', order.id, exc)

    # ── Render gang sheets ───────────────────────────────────────────────────
    gang_sheet_objs = []
    for sheet_idx, sheet_placements in enumerate(sheets, start=1):
        used_len = total_length(sheet_placements, margin)
        png_bytes = render_gang_sheet(
            placements=sheet_placements,
            design_files=design_file_map,
            sheet_length_in=min(used_len + margin, max_len),
            dpi=dpi,
            margin_in=margin,
        )
        gs = GangSheet(
            batch=batch,
            sheet_number=sheet_idx,
            width_inches=22,
            length_inches=round(min(used_len + margin, max_len), 2),
        )
        gs.file.save(
            f'batch_{batch.id}_sheet_{sheet_idx}.png',
            ContentFile(png_bytes),
            save=True,
        )
        gang_sheet_objs.append(gs)

    # ── Generate order slip PDFs (one per gang sheet) ────────────────────────
    for sheet_idx, (gs, sheet_placements) in enumerate(
        zip(gang_sheet_objs, sheets), start=1
    ):
        # Collect orders on this sheet
        order_ids_on_sheet = {p.order_id for p in sheet_placements}
        orders_with_seq = []
        for bo in batch_orders:
            if bo.order_id in order_ids_on_sheet:
                orders_with_seq.append((bo.sequence_number, bo.order))

        pdf_bytes = generate_slip_pdf(
            orders_with_seq=orders_with_seq,
            label_images=label_images,
        )
        slip = OrderSlipPDF(batch=batch, gang_sheet=gs)
        slip.file.save(
            f'batch_{batch.id}_slip_{sheet_idx}.pdf',
            ContentFile(pdf_bytes),
            save=True,
        )

    # Close file handles
    for f in design_file_map.values():
        if f:
            try:
                f.close()
            except Exception:
                pass

    # Update orders to IN_PRODUCTION
    order_ids = [bo.order_id for bo in batch_orders]
    Order.objects.filter(pk__in=order_ids).update(status=Order.Status.IN_PRODUCTION)
    for bo in batch_orders:
        log_event(bo.order, 'in_production', description=f'Batch #{batch.id}')
