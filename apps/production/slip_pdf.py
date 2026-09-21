"""
Order slip PDF generator (ReportLab).

One page (or more, for many-item orders) per order, ordered by sequence number:

  ┌──────────────────────────────────────────┐
  │        [ SHIPPING LABEL — picture ]      │  ← the only picture-only part
  ├──────────────────────────────────────────┤
  │ TIF_10 │ ATTENTION: gift message · note  │  ← yellow attention bar
  │ 4163893199  Jan Hanna  2026-09-03  …     │  ← order line (dark bar)
  ├──────────────────────────────────────────┤
  │ 1 [thumb]  Title / SKU / Size / Color    │  ← one box per item
  └──────────────────────────────────────────┘

Everything except the label and the thumbnails is real text on purpose: the PDFs
get shared through Google Drive, where searching an order number only finds it in
a PDF's text layer, never inside a picture.
"""
from __future__ import annotations

import io
import logging
import os
from typing import List, Optional

import requests
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image as RLImage,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)

PAGE_W, PAGE_H = letter  # 8.5" × 11"
MARGIN = 0.35 * inch
LABEL_MAX_H = 4.6 * inch
THUMB = 1.0 * inch

# ── Fonts ────────────────────────────────────────────────────────────────────
# reportlab bundles Bitstream Vera (permissive licence); unlike the built-in
# Helvetica it covers Turkish and other Latin accents, so buyer notes and names
# keep their text layer intact. Glyphs it lacks (emoji, CJK) are dropped.
_FONT_DIR = os.path.join(os.path.dirname(pdfmetrics.__file__), '..', 'fonts')
_FONT_DIR = os.path.normpath(_FONT_DIR)
pdfmetrics.registerFont(TTFont('Vera', os.path.join(_FONT_DIR, 'Vera.ttf')))
pdfmetrics.registerFont(TTFont('Vera-Bold', os.path.join(_FONT_DIR, 'VeraBd.ttf')))
pdfmetrics.registerFont(TTFont('Vera-Italic', os.path.join(_FONT_DIR, 'VeraIt.ttf')))
pdfmetrics.registerFont(TTFont('Vera-BoldItalic', os.path.join(_FONT_DIR, 'VeraBI.ttf')))
pdfmetrics.registerFontFamily(
    'Vera', normal='Vera', bold='Vera-Bold', italic='Vera-Italic', boldItalic='Vera-BoldItalic',
)
_GLYPHS = pdfmetrics.getFont('Vera').face.charToGlyph

STYLES = getSampleStyleSheet()
BASE = ParagraphStyle('base', parent=STYLES['Normal'], fontName='Vera', fontSize=8.5, leading=11)
TITLE = ParagraphStyle('title', parent=BASE, fontName='Vera-Bold', fontSize=9, leading=11)
NOTE = ParagraphStyle('note', parent=BASE, fontSize=8.5, leading=11)
BAR = ParagraphStyle('bar', parent=BASE, fontName='Vera-Bold', fontSize=11, leading=14, textColor=colors.white)
BAR_ORDER = ParagraphStyle('barorder', parent=BASE, fontName='Vera-Bold', fontSize=10, leading=13, textColor=colors.white)
ATTN = ParagraphStyle('attn', parent=BASE, fontName='Vera-Bold', fontSize=10, leading=13, textColor=colors.black)
QTY = ParagraphStyle('qty', parent=BASE, fontName='Vera-Bold', fontSize=20, leading=22, alignment=TA_CENTER)
SMALL = ParagraphStyle('small', parent=BASE, fontSize=7, leading=9, textColor=colors.grey)


def _txt(value) -> str:
    """Escape for reportlab's mini-markup and drop glyphs the font cannot draw."""
    text = '' if value is None else str(value)
    text = ''.join(ch for ch in text if ch in ('\n', '\t') or ord(ch) in _GLYPHS)
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br/>')


# ── Order content ────────────────────────────────────────────────────────────

def _physical_items(order):
    """Real products only — discount/adjustment lines (negative price) are not items."""
    return [i for i in order.items.select_related('pc_variant__pc_product').prefetch_related('designs')
            if i.unit_price >= 0]


def _attention_parts(order) -> List[str]:
    parts = []
    if any(i.personalization for i in order.items.all()):
        parts.append('Personalization')
    if order.is_gift or order.gift_message:
        parts.append('Gift message')
    if order.buyer_note:
        parts.append('Buyer note')
    if order.print_note:
        parts.append('Print note')
    return parts


def _fit(img_w, img_h, max_w, max_h):
    scale = min(max_w / img_w, max_h / img_h)
    return img_w * scale, img_h * scale


def _label_flowable(png_bytes, usable_w):
    if not png_bytes:
        t = Table([[Paragraph('NO LABEL — this order should not be in a batch', ATTN)]], colWidths=[usable_w], rowHeights=[1.2 * inch])
        t.setStyle(TableStyle([
            ('BOX', (0, 0), (-1, -1), 1.5, colors.red), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ]))
        return t
    with Image.open(io.BytesIO(png_bytes)) as im:
        w, h = _fit(im.width, im.height, usable_w, LABEL_MAX_H)
    img = RLImage(io.BytesIO(png_bytes), width=w, height=h)
    img.hAlign = 'CENTER'
    return img


_thumb_cache: dict = {}


def _thumbnail(item):
    """Small picture for an item: seller-uploaded image, else the marketplace
    listing photo (short timeout, never blocks the PDF), else the design file."""
    key = ('item', item.pk)
    if key in _thumb_cache:
        return _thumb_cache[key]

    raw = None
    try:
        if item.custom_image:
            with item.custom_image.open('rb') as fh:
                raw = fh.read()
        elif item.image_url:
            resp = requests.get(item.image_url, timeout=4)
            if resp.ok:
                raw = resp.content
        if raw is None:
            design = item.designs.first()
            if design and design.file:
                with design.file.open('rb') as fh:
                    raw = fh.read()
    except Exception:
        raw = None

    flowable = None
    if raw:
        try:
            with Image.open(io.BytesIO(raw)) as im:
                im.thumbnail((360, 360))
                rgba = im.convert('RGBA')
                bg = Image.new('RGB', rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[3])
                out = io.BytesIO()
                bg.save(out, format='JPEG', quality=80)
                w, h = _fit(bg.width, bg.height, THUMB, THUMB)
                flowable = RLImage(io.BytesIO(out.getvalue()), width=w, height=h)
        except Exception:
            flowable = None
    _thumb_cache[key] = flowable
    return flowable or Paragraph('', SMALL)


def _item_block(item, order, usable_w):
    variant = item.pc_variant
    product = variant.pc_product.name if variant else item.raw_product_name
    size = variant.size if variant else ''
    color = variant.color if variant else ''
    options = ', '.join(f'{k}: {v}' for k, v in (item.raw_options or {}).items())

    lines = [f'<b>{_txt(item.raw_product_name)}</b>']
    if product and product != item.raw_product_name:
        lines.append(f'Product: {_txt(product)}')
    lines.append(f'SKU: <b>{_txt(item.marketplace_sku or "-")}</b>')
    if size or color:
        lines.append(f'Size: <b>{_txt(size or "-")}</b>   Color: <b>{_txt(color or "-")}</b>')
    elif options:
        lines.append(_txt(options))
    if item.personalization:
        lines.append(f'<font color="#D40000"><b>Personalization:</b> {_txt(item.personalization)}</font>')
    lines.append(f'Order: {_txt(order.shipstation_order_number or order.external_order_number)}   Qty: <b>{item.quantity}</b>')

    row = [[
        Paragraph(str(item.quantity), QTY),
        _thumbnail(item),
        Paragraph('<br/>'.join(lines), BASE),
    ]]
    t = Table(row, colWidths=[0.45 * inch, THUMB + 0.1 * inch, usable_w - 0.45 * inch - THUMB - 0.1 * inch])
    t.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOX', (0, 0), (-1, -1), 0.75, colors.black),
        ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    return t


def _header(tif, order, usable_w):
    attention = ' · '.join(_attention_parts(order))
    top = Table(
        [[Paragraph(f'TIF_{tif}', BAR), Paragraph(f'ATTENTION: {_txt(attention)}', ATTN)]],
        colWidths=[1.0 * inch, usable_w - 1.0 * inch],
    )
    top.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#888888')),
        ('BACKGROUND', (1, 0), (1, 0), colors.HexColor('#FFFF00')),
        ('BOX', (0, 0), (-1, -1), 0.75, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))

    date = order.ordered_at.strftime('%Y-%m-%d') if order.ordered_at else ''
    total_items = sum(i.quantity for i in _physical_items(order))
    number = order.shipstation_order_number or order.external_order_number
    info = Paragraph(
        f'{_txt(number)}&nbsp;&nbsp;&nbsp;{_txt(order.customer_name)}&nbsp;&nbsp;&nbsp;{date}'
        f'&nbsp;&nbsp;&nbsp;Total Item: {total_items}&nbsp;&nbsp;&nbsp;Store: {_txt(order.store.name)}',
        BAR_ORDER,
    )
    bar = Table([[info]], colWidths=[usable_w])
    bar.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#888888')),
        ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    return [top, bar]


def _build_order(story, tif, order, label_png, usable_w):
    story.append(_label_flowable(label_png, usable_w))
    story.append(Spacer(1, 10))
    story.extend(_header(tif, order, usable_w))
    story.append(Spacer(1, 4))

    for item in _physical_items(order):
        story.append(_item_block(item, order, usable_w))
        story.append(Spacer(1, 3))

    notes = []
    if order.print_note:
        notes.append(f'<b>Print note:</b> {_txt(order.print_note)}')
    if order.buyer_note:
        notes.append(f'<b>Buyer note:</b> {_txt(order.buyer_note)}')
    if order.gift_message:
        notes.append(f'<b>Gift message:</b> {_txt(order.gift_message)}')
    for text in notes:
        story.append(Paragraph(text, NOTE))
        story.append(Spacer(1, 2))


def _preview_pages(story, preview, tif, usable_w):
    """Page(s) showing the sheet with order numbers on each design, sliced so a long
    sheet continues over several pages instead of shrinking to nothing."""
    max_h = PAGE_H - 2 * MARGIN - 0.6 * inch
    chunk_px = max(1, int(preview.width * (max_h / usable_w)))
    total = -(-preview.height // chunk_px)
    for i in range(total):
        piece = preview.crop((0, i * chunk_px, preview.width, min(preview.height, (i + 1) * chunk_px)))
        out = io.BytesIO()
        piece.save(out, format='JPEG', quality=85)
        w, h = _fit(piece.width, piece.height, usable_w, max_h)
        bar = Table([[Paragraph(f'TIF_{tif}' + (f'   (part {i + 1}/{total})' if total > 1 else ''), BAR)]], colWidths=[usable_w])
        bar.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#888888'))]))
        story.append(bar)
        story.append(Spacer(1, 4))
        story.append(RLImage(io.BytesIO(out.getvalue()), width=w, height=h))
        story.append(PageBreak())


def generate_slip_pdf(
    orders: List,                           # order objects in the order they appear on the sheet
    sheet_number: int,                      # the gang sheet's number — printed as TIF_<n> on every page
    label_images: Optional[dict] = None,    # {order_id: PNG bytes}; falls back to the stored label file
    preview=None,                           # PIL image of the sheet with order numbers (first page(s))
) -> bytes:
    """Slip PDF for ONE gang sheet: optional sheet preview page(s), then one page per
    order (more for big orders). Every page carries the sheet's TIF number, exactly as
    the film file is numbered, so film and paperwork stay paired."""
    from apps.shipping.labels import load_label_png

    _thumb_cache.clear()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN,
        title=f'TIF_{sheet_number} order slips',
    )
    usable_w = PAGE_W - 2 * MARGIN

    story = []
    if preview is not None:
        _preview_pages(story, preview, sheet_number, usable_w)
    for order in orders:
        label_png = (label_images or {}).get(order.id) or load_label_png(order)
        _build_order(story, sheet_number, order, label_png, usable_w)
        story.append(PageBreak())

    doc.build(story)
    return buf.getvalue()
