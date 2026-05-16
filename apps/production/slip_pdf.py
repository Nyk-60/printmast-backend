"""
Order slip PDF generator using ReportLab.

Layout per page (one page = one order):
  ┌─────────────────────────────────┐
  │  #SEQ  ORDER# | CUSTOMER | DATE │  ← header bar
  ├─────────────────────────────────┤
  │  [LABEL PLACEHOLDER / IMAGE]    │  ← shipping label area (40% height)
  ├─────────────────────────────────┤
  │  Item 1: [thumb] SKU  Size  Col │  ← order items
  │  Item 2: [thumb] ...            │
  └─────────────────────────────────┘
"""
from __future__ import annotations

import io
from typing import List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image as RLImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER


PAGE_W, PAGE_H = letter  # 8.5" × 11"
MARGIN = 0.4 * inch
STYLES = getSampleStyleSheet()

HEADER_STYLE = ParagraphStyle(
    'header', parent=STYLES['Normal'],
    fontSize=11, leading=14, textColor=colors.white,
    fontName='Helvetica-Bold',
)
NORMAL = ParagraphStyle(
    'norm', parent=STYLES['Normal'], fontSize=9, leading=12,
)
SMALL = ParagraphStyle(
    'small', parent=STYLES['Normal'], fontSize=8, leading=10,
    textColor=colors.grey,
)
BOLD = ParagraphStyle(
    'bold', parent=STYLES['Normal'], fontSize=9, leading=12,
    fontName='Helvetica-Bold',
)
ATTN_STYLE = ParagraphStyle(
    'attn', parent=STYLES['Normal'], fontSize=10, leading=13,
    fontName='Helvetica-Bold', textColor=colors.HexColor('#D40000'),
    backColor=colors.HexColor('#FFFF00'),
)


def _attention_flag(order) -> bool:
    """Mark order as ATTENTION if it has personalization or a print note."""
    if order.print_note:
        return True
    for item in order.items.all():
        if item.personalization:
            return True
    return False


def generate_slip_pdf(
    orders_with_seq: List[tuple],   # [(seq_number, order_obj), ...]
    label_images: Optional[dict] = None,  # {order_id: bytes (PNG/PDF label)}
) -> bytes:
    """
    Generate a multi-page order slip PDF.
    One page per order; pages ordered by sequence number.

    label_images: optional dict of order_id → raw label bytes (PNG preferred).
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )

    story = []

    for seq, order in sorted(orders_with_seq, key=lambda x: x[0]):
        is_attn = _attention_flag(order)
        _build_order_page(story, seq, order, label_images, is_attn)
        story.append(_PageBreak())

    doc.build(story)
    buf.seek(0)
    return buf.read()


def _build_order_page(story, seq, order, label_images, is_attn):
    usable_w = PAGE_W - 2 * MARGIN

    # ── Header bar ──────────────────────────────────────────────────────────
    attn_text = '<font color="#D40000"><b>ATTENTION  </b></font>' if is_attn else ''
    header_data = [[
        Paragraph(
            f'{attn_text}TIF_{seq}   '
            f'<font size="9">{order.shipstation_order_number}</font>   '
            f'<font size="9">{order.customer_name}</font>   '
            f'<font size="9">{order.ordered_at.strftime("%Y-%m-%d") if order.ordered_at else ""}</font>   '
            f'<font size="9">Total Items: {order.items.count()}</font>   '
            f'<font size="9">Store: {order.store.name}</font>',
            HEADER_STYLE,
        )
    ]]
    header_table = Table(header_data, colWidths=[usable_w])
    header_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#222222')),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 4))

    # ── Shipping label area ──────────────────────────────────────────────────
    label_h = 3.2 * inch
    label_bytes = (label_images or {}).get(order.id)
    if label_bytes:
        try:
            img_buf = io.BytesIO(label_bytes)
            label_img = RLImage(img_buf, width=usable_w, height=label_h)
            story.append(label_img)
        except Exception:
            story.append(_label_placeholder(usable_w, label_h))
    else:
        story.append(_label_placeholder(usable_w, label_h))

    story.append(Spacer(1, 6))

    # ── Ship-to address ──────────────────────────────────────────────────────
    addr_lines = [
        order.ship_to_name,
        order.ship_to_address1,
        order.ship_to_address2,
        f'{order.ship_to_city}, {order.ship_to_state} {order.ship_to_postal_code}',
        order.ship_to_country,
    ]
    addr_text = '<br/>'.join(l for l in addr_lines if l)
    story.append(Paragraph(addr_text, NORMAL))
    story.append(Spacer(1, 6))

    # ── Order items ──────────────────────────────────────────────────────────
    for item in order.items.select_related('pc_variant__pc_product').prefetch_related('designs'):
        _build_item_row(story, item, usable_w)
        story.append(Spacer(1, 3))

    # ── Print note ───────────────────────────────────────────────────────────
    if order.print_note:
        story.append(Paragraph(f'<b>Print Note:</b> {order.print_note}', ATTN_STYLE))

    if order.buyer_note:
        story.append(Paragraph(f'<b>Buyer Note:</b> {order.buyer_note}', SMALL))


def _build_item_row(story, item, usable_w):
    variant = item.pc_variant
    product_name = variant.pc_product.name if variant else item.raw_product_name
    size = variant.size if variant else ''
    color = variant.color if variant else ''

    options_str = ', '.join(f'{k}: {v}' for k, v in (item.raw_options or {}).items())

    # Thumbnail from first design
    thumb_cell = Paragraph('(no design)', SMALL)
    first_design = item.designs.first()
    if first_design and first_design.file:
        try:
            img = RLImage(first_design.file.path, width=0.6 * inch, height=0.6 * inch)
            thumb_cell = img
        except Exception:
            pass

    detail_text = (
        f'<b>{product_name}</b><br/>'
        f'SKU: {item.marketplace_sku or "—"}  '
        f'Size: {size}  Color: {color}<br/>'
        f'{options_str}'
    )
    if item.personalization:
        detail_text += f'<br/><font color="#D40000"><b>Personalization: {item.personalization}</b></font>'

    col_widths = [0.75 * inch, usable_w - 0.75 * inch - 0.6 * inch, 0.6 * inch]
    row_data = [
        [thumb_cell, Paragraph(detail_text, NORMAL), Paragraph(f'×{item.quantity}', BOLD)]
    ]
    t = Table(row_data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.lightgrey),
    ]))
    story.append(t)


def _label_placeholder(w, h):
    """Grey box placeholder when label is not yet purchased."""
    data = [[Paragraph('[ SHIPPING LABEL ]', ParagraphStyle(
        'ph', parent=STYLES['Normal'], fontSize=14, alignment=TA_CENTER,
        textColor=colors.grey,
    ))]]
    t = Table(data, colWidths=[w], rowHeights=[h])
    t.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 1, colors.lightgrey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    return t


class _PageBreak:
    """Minimal ReportLab flowable for a page break."""
    def wrap(self, *_):
        return 0, 0

    def draw(self):
        pass

    def splitOn(self, *_):
        return [self]

    def getSpaceBefore(self):
        return 0

    def getSpaceAfter(self):
        return 0

    def identity(self, *_):
        return 'PageBreak'


# Use the built-in PageBreak instead
from reportlab.platypus import PageBreak
_PageBreak = PageBreak
