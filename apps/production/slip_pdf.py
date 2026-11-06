"""
Order slip PDF generator (ReportLab).

US Letter, split by the label stock's own horizontal perforation at the
vertical center (5.5"): the shipping label sits in the top half (rotated 90°
so its own top edge faces left, bottom edge faces right — label text reads
left-to-right across the fold), the order's items sit in the bottom half as
a grid of cards that always fills the full available area regardless of how
many items there are:

  ┌──────────────────────────────────────────┐
  │ [rotated]                                 │
  │ [ shipping  ]                             │  top half: label only
  │ [  label    ]                             │
  - - - - - - - - - - - - - - - - - - - - - - -   ← perforation (5.5" mark)
  │ TIF_10   ATTENTION: gift message · note   │
  │ 4163893199  Jan Hanna  2026-09-03  …      │  bottom half: order info +
  │ ┌──────────┐┌──────────┐                  │  an adaptive item-card grid
  │ │  item 1  ││  item 2  │                   │  (1 item -> one big card
  │ └──────────┘└──────────┘                  │   filling the whole area,
  └──────────────────────────────────────────┘   6 -> a 2x3 grid, etc.)

An order with more items than fit on one page spans several of these pages
(label + header repeated on each), each page packed to its per-page capacity
before overflowing to the next — a non-last page of a multi-page order also
gets a small "go to the next page" marker in the right margin. 7+ item orders
switch every one of their pages to a smaller card (see SHRINK_SCALE in
_paginate_order) so more fit per page, instead of fewer, larger pages.

Everything except the label and the thumbnails is real text on purpose: the
PDFs get shared through Google Drive, where searching an order number only
finds it in a PDF's text layer, never inside a picture.
"""
from __future__ import annotations

import io
import logging
import os
from typing import List, Optional

import requests
from PIL import Image, ImageDraw
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.graphics.shapes import Circle, Drawing, Image as GraphicsImage, Line, String
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    FrameBreak,
    Image as RLImage,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)

PAGE_W, PAGE_H = letter  # 8.5" × 11"
MARGIN = 0.3 * inch
TEAR_GAP = 0.12 * inch          # breathing room above the perforation (label side)
HEADER_TEAR_GAP = TEAR_GAP / 2  # breathing room below the perforation (header side) — half as much
HALF_H = PAGE_H / 2             # perforation sits at the physical middle of the sheet
TOP_FRAME_H = HALF_H - MARGIN - TEAR_GAP            # label area
BOTTOM_FRAME_H = HALF_H - MARGIN - HEADER_TEAR_GAP  # order info + item grid area
USABLE_W = PAGE_W - 2 * MARGIN

# Item row (photo + badge + text; the on-garment mockup graphic is a later addition,
# left blank for now). Photo box measured directly off the user's Canva reference.
PHOTO_W = 1.29 * inch
PHOTO_H = 1.42 * inch
RADIUS_RATIO = 0.05  # photo corner radius as a fraction of its own height — scales with photo size
RADIUS_MIN, RADIUS_MAX = 4, 10  # pt — a subtle but actually-visible rounding at any photo size
ITEM_QTY_D = 18  # pt — qty badge diameter at the PHOTO_H reference size, overlaid on the photo
ROW_GAP = 8  # pt — gap between photo and text column (never flush against it)
PHOTO_INSET = 2  # pt — tiny gap between a card's border and its photo (dense grid only)
SEP_W = 0.25  # pt — thinnest practical hairline around/between item cards
PHOTO_ASPECT = PHOTO_W / PHOTO_H  # width:height stays constant as the photo scales up/down
QTY_D_RATIO = ITEM_QTY_D / PHOTO_H  # badge stays proportional to the photo at any size

# Every item card is the same fixed size regardless of how many are on the
# page — confirmed against a real render: an adaptive size that grows to fill
# the whole page for a single item looked oversized once actually seen. 1-3
# items: single column, one full-width card per item. 4+ items: the same
# card, in a 2-column grid instead of one long column (how many fit per page
# is measured at build time — see _build_order — not a fixed guess here).
CARD_H = 1.3 * inch
MAX_BIG_ITEMS = 3
DENSE_GUTTER = 20  # pt — gap between the two grid columns; cards still sit flush to the margins
CARD_GAP = 2  # pt — vertical gap between stacked cards/rows
HEADER_CARD_GAP = 6  # pt — gap between the header's bottom rule and the first card's top border
# 7+ item orders (more than the normal dense grid's per-page capacity) switch
# every one of their pages to this smaller card — photo, badge, gaps and fonts
# all scaled down together, not just the photo — so more fit per page (8 vs.
# 6 at the normal size); see _paginate_order for the capacity math.
SHRINK_SCALE = 0.80

TEXT_SMALL = 8.0  # pt, fixed — raw marketplace title / SKU value / personalization value — the anchor
TEXT_LARGE = TEXT_SMALL + 1.5  # pt, fixed — catalog name / SKU label / Size / Color — always 1.5pt above small

# ── Fonts ────────────────────────────────────────────────────────────────────
# Arimo (Google's open-licence, Arial-metric-compatible family) — confirmed to
# fully cover Turkish (ğşıöüç) and French-accented (éèêëàâîïôûù) glyphs, unlike
# relying on the built-in Helvetica. Static weights only (not the variable-font
# build), since reportlab can't use a variable font's weight axis.
_FONT_DIR = os.path.join(os.path.dirname(__file__), 'fonts')
pdfmetrics.registerFont(TTFont('Arimo', os.path.join(_FONT_DIR, 'Arimo-Regular.ttf')))
pdfmetrics.registerFont(TTFont('Arimo-Bold', os.path.join(_FONT_DIR, 'Arimo-Bold.ttf')))
pdfmetrics.registerFont(TTFont('Arimo-Italic', os.path.join(_FONT_DIR, 'Arimo-Italic.ttf')))
pdfmetrics.registerFont(TTFont('Arimo-BoldItalic', os.path.join(_FONT_DIR, 'Arimo-BoldItalic.ttf')))
pdfmetrics.registerFontFamily(
    'Arimo', normal='Arimo', bold='Arimo-Bold', italic='Arimo-Italic', boldItalic='Arimo-BoldItalic',
)
_GLYPHS = pdfmetrics.getFont('Arimo').face.charToGlyph

STYLES = getSampleStyleSheet()
BASE = ParagraphStyle('base', parent=STYLES['Normal'], fontName='Arimo', fontSize=8.5, leading=11)
TITLE = ParagraphStyle('title', parent=BASE, fontName='Arimo-Bold', fontSize=9, leading=11)
BAR = ParagraphStyle('bar', parent=BASE, fontName='Arimo-Bold', fontSize=11, leading=14, textColor=colors.white)
BAR_ORDER = ParagraphStyle('barorder', parent=BASE, fontName='Arimo-Bold', fontSize=10, leading=13, textColor=colors.white)
ATTN = ParagraphStyle('attn', parent=BASE, fontName='Arimo-Bold', fontSize=10, leading=13, textColor=colors.black)
QTY = ParagraphStyle('qty', parent=BASE, fontName='Arimo-Bold', fontSize=20, leading=22, alignment=TA_CENTER)
SMALL = ParagraphStyle('small', parent=BASE, fontSize=7, leading=9, textColor=colors.grey)
HEADER = ParagraphStyle('header', parent=BASE, fontName='Arimo-Bold', fontSize=10.5, leading=13, alignment=TA_LEFT)


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




def _fit(img_w, img_h, max_w, max_h):
    scale = min(max_w / img_w, max_h / img_h)
    return img_w * scale, img_h * scale


def _label_flowable(png_bytes, usable_w, max_h):
    """The label is rotated 90° so its own top edge faces left and its bottom
    (tracking barcode) faces right, to lie flat across the top of the page."""
    if not png_bytes:
        t = Table([[Paragraph('NO LABEL — this order should not be in a batch', ATTN)]],
                   colWidths=[usable_w], rowHeights=[min(1.2 * inch, max_h)])
        t.setStyle(TableStyle([
            ('BOX', (0, 0), (-1, -1), 1.5, colors.red), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ]))
        return t
    with Image.open(io.BytesIO(png_bytes)) as im:
        rotated = im.rotate(90, expand=True)
        buf = io.BytesIO()
        rotated.save(buf, format='PNG')
        rot_bytes = buf.getvalue()
        w, h = _fit(rotated.width, rotated.height, usable_w, max_h)
    img = RLImage(io.BytesIO(rot_bytes), width=w, height=h)
    img.hAlign = 'CENTER'
    return img


_thumb_raw_cache: dict = {}


def _thumbnail_processed(item):
    """Decoded, background-filled JPEG bytes for an item's thumbnail source —
    seller-uploaded image, else the marketplace listing photo (short timeout,
    never blocks the PDF), else the design file. Cached once per item at a
    resolution high enough to downscale cleanly to whatever card size this
    PDF ends up needing (card size varies with how many items are on a page)."""
    key = item.pk
    if key in _thumb_raw_cache:
        return _thumb_raw_cache[key]

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

    out_bytes = None
    if raw:
        try:
            with Image.open(io.BytesIO(raw)) as im:
                im.thumbnail((700, 700))
                rgba = im.convert('RGBA')
                bg = Image.new('RGB', rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[3])
                out = io.BytesIO()
                bg.save(out, format='JPEG', quality=85)
                out_bytes = out.getvalue()
        except Exception:
            out_bytes = None
    _thumb_raw_cache[key] = out_bytes
    return out_bytes


def _photo_cover(flat_jpeg_bytes, box_w_pt, box_h_pt, radius_pt, dpi=150):
    """Scales to COVER the box (cropping overflow, centered) rather than
    letterboxing — real order photos come in all sorts of aspect ratios, but
    the photo box itself is a fixed size, so this is the only way to fill it
    without stretching. Corners are rounded by pasting the crop onto an
    opaque white canvas through a rounded-rect mask: reportlab's renderPDF
    ignores a PIL image's alpha channel when drawn via graphics.shapes.Image,
    so the corners must be baked in as actual white pixels, not transparency."""
    px_w, px_h = max(1, round(box_w_pt / 72 * dpi)), max(1, round(box_h_pt / 72 * dpi))
    with Image.open(io.BytesIO(flat_jpeg_bytes)) as im:
        img_w, img_h = im.size
        scale = max(px_w / img_w, px_h / img_h)
        new_w, new_h = max(1, round(img_w * scale)), max(1, round(img_h * scale))
        resized = im.resize((new_w, new_h), Image.LANCZOS)
        left, top = (new_w - px_w) // 2, (new_h - px_h) // 2
        cropped = resized.crop((left, top, left + px_w, top + px_h)).convert('RGB')
    radius_px = max(0, round(radius_pt / 72 * dpi))
    mask = Image.new('L', (px_w, px_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, px_w, px_h], radius=radius_px, fill=255)
    canvas = Image.new('RGB', (px_w, px_h), (255, 255, 255))
    canvas.paste(cropped, (0, 0), mask)
    return canvas


_item_photo_cache: dict = {}


def _item_photo_with_badge(item, photo_w=PHOTO_W, photo_h=PHOTO_H, qty_d=ITEM_QTY_D):
    """One Drawing: the cover-cropped, rounded-corner product photo with the
    qty badge (white circle, black number, thin black outline for contrast
    against light photos) overlaid flush to its left edge, a little above
    center — matching the user's own Canva placement. photo_w/photo_h vary
    (big single-column rows vs. the dense small-card grid), so the cover-crop
    — and its cache key — are keyed by the actual size requested."""
    radius = min(RADIUS_MAX, max(RADIUS_MIN, photo_h * RADIUS_RATIO))
    key = (item.pk, round(photo_w), round(photo_h))
    if key not in _item_photo_cache:
        raw = _thumbnail_processed(item)
        _item_photo_cache[key] = _photo_cover(raw, photo_w, photo_h, radius) if raw else None
    photo = _item_photo_cache[key]

    d = Drawing(photo_w, photo_h)
    if photo is not None:
        d.add(GraphicsImage(0, 0, photo_w, photo_h, photo))
    else:
        d.add(Circle(photo_w / 2, photo_h / 2, 1, fillColor=colors.white, strokeColor=colors.grey))

    cx, cy = qty_d / 2, photo_h * 0.75
    d.add(Circle(cx, cy, qty_d / 2, fillColor=colors.white, strokeColor=colors.black, strokeWidth=0.75))
    d.add(String(cx, cy - qty_d * 0.19, str(item.quantity), fontName='Arimo-Bold', fontSize=qty_d * 0.606,
                 fillColor=colors.black, textAnchor='middle'))
    return d


def _paginate_max_pack(items, max_per_page):
    """Fills each page to max_per_page items — only the last page may come up
    short. Used once an order's switched to the smaller 7+ item card (see
    _paginate_order): the point there is showing as many items per page as
    that size allows, not keeping every page's count visually even."""
    n = len(items)
    if n == 0:
        return [items]
    return [items[i:i + max_per_page] for i in range(0, n, max_per_page)]


def _clamp(text, col_w, font_name, font_size, max_lines):
    """Marketplace listing titles are often SEO-stuffed and arbitrarily long;
    at a multi-item card's narrower width that can wrap into enough lines to
    push the card's content past its (fixed, frame-filling) row height. Greedily
    wraps using the font's REAL measured width (pdfmetrics.stringWidth) rather
    than a chars-per-line guess, so the result never silently wraps into more
    than max_lines once it reaches the Paragraph — cuts at a word boundary and
    ellipsizes once it would. The exact catalog product/SKU/size/color below it
    stays intact either way, so nothing operationally important is ever cut."""
    text = (text or '').strip()
    if not text:
        return ''
    lines, cur, words, i = [], '', text.split(), 0
    while i < len(words) and len(lines) < max_lines:
        trial = f'{cur} {words[i]}'.strip()
        if not cur or pdfmetrics.stringWidth(trial, font_name, font_size) <= col_w:
            cur = trial
            i += 1
        else:
            lines.append(cur)
            cur = ''
    if cur and len(lines) < max_lines:
        lines.append(cur)
        cur = ''
    truncated = i < len(words) or bool(cur)
    if truncated:
        last = lines[-1] if lines else ''
        while last and pdfmetrics.stringWidth(last + '…', font_name, font_size) > col_w:
            last = last[:-1].rstrip()
        lines = lines[:-1] + [(last or (lines[-1] if lines else '')) + '…']
    return '<br/>'.join(_txt(l) for l in lines)


def _item_text_lines(item):
    """Ordered list of (kind, label, value) describing one item's text block.
    kind: 'title' (raw marketplace listing title — demoted, least important),
    'catalog' (the PC's own clean product name — promoted), 'sku', 'field'
    (Size/Color), 'extra' (Personalization and, as Etsy adds more of these,
    whatever other named custom-text fields come with the order — rendered
    identically and in marketplace order; only today's single flat
    `personalization` field actually exists, but the renderer below already
    treats this as a list so a future multi-field source is a one-line change)."""
    variant = item.pc_variant
    product = variant.pc_product.name if variant else None
    size = variant.size if variant else ''
    color = variant.color if variant else ''

    lines = [('title', None, item.raw_product_name or '')]
    if product:
        lines.append(('catalog', None, product))
    lines.append(('sku', 'SKU:', item.marketplace_sku or '-'))
    if size:
        lines.append(('field', 'Size:', size))
    if color:
        lines.append(('field', 'Color:', color))
    if item.personalization:
        lines.append(('extra', 'Personalization:', item.personalization))
    if not variant and not size and not color:
        for k, v in (item.raw_options or {}).items():
            if k.strip().lower() in ('size', 'color'):
                continue
            lines.append(('extra', f'{k}:', str(v)))
    return lines


def _render_text_block(lines, scale, max_w):
    large, small = TEXT_LARGE * scale, TEXT_SMALL * scale
    lead_l, lead_s = large * 1.25, small * 1.25

    # The demoted raw title's own width is capped to wherever the catalog name
    # line ends, so it never visually overhangs past the (real) product name
    # below it — not just clamped to the full column width like every other line.
    catalog_value = next((v for k, _, v in lines if k == 'catalog'), None)
    if catalog_value:
        _, catalog_w = _fit_line(catalog_value, 'Arimo-Bold', large, max_w)
        title_cap = max(20, catalog_w)
    else:
        # No catalog name to measure against (unmatched item) — still don't
        # let the title run the full column width; approximate a typical
        # product-name length instead.
        title_cap = max(20, max_w * 0.5)

    out = []
    for kind, label, value in lines:
        if kind == 'title':
            style = ParagraphStyle(f'it_title_{scale}', parent=BASE, fontName='Arimo', fontSize=small, leading=lead_s)
            out.append(Paragraph(_clamp(value, title_cap, 'Arimo', small, max_lines=1), style))
        elif kind == 'catalog':
            style = ParagraphStyle(f'it_cat_{scale}', parent=BASE, fontName='Arimo-Bold', fontSize=large, leading=lead_l)
            out.append(Paragraph(f'<u>{_clamp(value, max_w, "Arimo-Bold", large, max_lines=1)}</u>', style))
        elif kind == 'field':
            style = ParagraphStyle(f'it_field_{scale}', parent=BASE, fontName='Arimo-Bold', fontSize=large, leading=lead_l)
            out.append(Paragraph(_clamp(f'{label} {value}', max_w, 'Arimo-Bold', large, max_lines=1), style))
        else:  # 'sku' or 'extra': label and value both plain (not bold) AND both small
            style = ParagraphStyle(f'it_mix_{scale}', parent=BASE, fontSize=small, leading=lead_s)
            label_w = pdfmetrics.stringWidth(label + ' ', 'Arimo', small)
            val = _clamp(value, max(10, max_w - label_w), 'Arimo', small, max_lines=2)
            markup = f'<font name="Arimo" size="{small}">{_txt(label)} </font><font name="Arimo" size="{small}">{val}</font>'
            out.append(Paragraph(markup, style))
    return out


def _more_marker(scale):
    size = TEXT_SMALL * scale
    style = ParagraphStyle('it_more', parent=BASE, fontSize=size, leading=size * 1.25, textColor=colors.grey)
    return Paragraph('…', style)


def _fit_text_block(lines, max_w, max_h, scale=1.0):
    """Font sizes are fixed at TEXT_LARGE/TEXT_SMALL * scale (scale is 1.0
    except for a 7+ item order's smaller card — see _paginate_order) — no
    further per-item shrinking or growing. If the full set of lines doesn't
    fit at that size — a lot of personalization-style fields — 'extra' lines
    (the core fields: title/catalog/SKU/size/color never drop) are dropped
    from the end until it does, with a single '…' appended once something was
    actually hidden. This text is only a quick reference for whoever preps
    the design; the full detail always exists in the app panel, so trimming
    here costs nothing real. Aligned to the top of the row, not vertically
    centered."""
    flowables = _render_text_block(lines, scale, max_w)
    if sum(f.wrap(max_w, max_h)[1] for f in flowables) <= max_h:
        return flowables

    core = [l for l in lines if l[0] != 'extra']
    all_extras = [l for l in lines if l[0] == 'extra']
    extras = list(all_extras)
    while extras:
        flowables = _render_text_block(core + extras, scale, max_w)
        if sum(f.wrap(max_w, max_h)[1] for f in flowables) <= max_h:
            if len(extras) < len(all_extras):
                flowables.append(_more_marker(scale))
            return flowables
        extras = extras[:-1]

    flowables = _render_text_block(core, scale, max_w)
    if all_extras:
        flowables.append(_more_marker(scale))
    return flowables


def _item_row(item, row_w, card_h=CARD_H, scale=1.0):
    """One fixed-size (card_h) card: photo+badge on the left with text filling
    the rest, dashed box border. Used identically whether it's stacked one-
    per-row (1-3 items) or arranged 2-per-row in the dense grid (4+) — same
    card either way, only the arrangement around it differs. A 7+ item order
    passes a smaller card_h/scale (see _paginate_order) so more fit per page —
    photo, badge, gaps and fonts all shrink together, not just the photo. The
    on-garment mockup graphic is a later, separate addition — its width/
    position aren't decided yet, so the text column simply uses all remaining
    width for now."""
    inset = PHOTO_INSET * scale
    row_gap = ROW_GAP * scale
    photo_h = card_h - 2 * inset
    photo_w = photo_h * PHOTO_ASPECT
    qty_d = photo_h * QTY_D_RATIO
    photo = _item_photo_with_badge(item, photo_w, photo_h, qty_d)
    text_w = row_w - inset - photo_w - row_gap - inset
    text_flowables = _fit_text_block(_item_text_lines(item), text_w, card_h - 2 * inset, scale=scale)

    # Explicit bottom padding that soaks up every bit of unused height, rather
    # than top=0/bottom=0 and trusting the row to stay flush at the top: when
    # a cell's declared content (padding + flowables) is shorter than the row,
    # reportlab's own default for the leftover space turns out to anchor it
    # toward the BOTTOM of the row (confirmed empirically, at any row size) —
    # so a short text block with no bottom padding to claim that leftover
    # renders bottom-heavy despite TOPPADDING already being 0.
    content_h = sum(f.wrap(text_w, card_h)[1] for f in text_flowables)
    text_bottom_pad = max(0, card_h - content_h)

    t = Table([[photo, text_flowables]], colWidths=[inset + photo_w + row_gap, text_w + inset], rowHeights=[card_h])
    t.setStyle(TableStyle([
        ('LEFTPADDING', (0, 0), (0, 0), inset), ('RIGHTPADDING', (0, 0), (0, 0), row_gap),
        ('LEFTPADDING', (1, 0), (1, 0), 0), ('RIGHTPADDING', (1, 0), (1, 0), inset),
        ('TOPPADDING', (0, 0), (0, 0), inset), ('BOTTOMPADDING', (0, 0), (0, 0), inset),
        ('TOPPADDING', (1, 0), (1, 0), 0), ('BOTTOMPADDING', (1, 0), (1, 0), text_bottom_pad),
        ('BOX', (0, 0), (-1, -1), SEP_W, colors.black, 0, (2, 2)),
    ]))
    return t


def _item_rows_big(items, usable_w) -> List:
    """1-3 items: single column, one fixed-size card per item, stacked with a
    CARD_GAP between them (same gap as header-bottom-line to the first card)."""
    story = []
    for i, item in enumerate(items):
        if i > 0:
            story.append(Spacer(1, CARD_GAP))
        story.append(_item_row(item, usable_w))
    return story


def _item_rows_dense(items, usable_w, card_h=CARD_H, scale=1.0) -> List:
    """4+ items: the same card (optionally at the smaller 7+ item SHRINK_SCALE
    size — see _paginate_order), 2-column grid instead of one long column —
    otherwise identical card. The row gap scales along with the card so it
    stays proportional."""
    gutter = DENSE_GUTTER * scale
    card_gap = CARD_GAP * scale
    card_w = (usable_w - gutter) / 2
    story = []
    for row_i, i in enumerate(range(0, len(items), 2)):
        if row_i > 0:
            story.append(Spacer(1, card_gap))
        pair = items[i:i + 2]
        if len(pair) == 2:
            cells = [_item_row(pair[0], card_w, card_h, scale), '', _item_row(pair[1], card_w, card_h, scale)]
            widths = [card_w, gutter, card_w]
        else:
            cells = [_item_row(pair[0], card_w, card_h, scale)]
            widths = [card_w]
        row = Table([cells], colWidths=widths)
        row.setStyle(TableStyle([
            ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        story.append(row)
    return story


DOT_D = 6  # separator dot diameter (pt)
BADGE_D = 15  # "Order Total" count badge diameter (pt)


def _dot():
    d = Drawing(DOT_D, DOT_D)
    d.add(Circle(DOT_D / 2, DOT_D / 2, DOT_D / 2, fillColor=colors.black, strokeColor=None))
    return d


def _total_badge(count):
    d = Drawing(BADGE_D, BADGE_D)
    d.add(Circle(BADGE_D / 2, BADGE_D / 2, BADGE_D / 2, fillColor=colors.black, strokeColor=None))
    d.add(String(BADGE_D / 2, BADGE_D / 2 - 3.3, str(count), fontName='Arimo-Bold', fontSize=9.5,
                 fillColor=colors.white, textAnchor='middle'))
    return d


def _order_date_str(order):
    if not order.ordered_at:
        return ''
    return f'{order.ordered_at.strftime("%B")} {order.ordered_at.day}, {order.ordered_at.year}'


def _sub_user_name(order):
    if order.sub_user_id and order.sub_user.user_id:
        user = order.sub_user.user
        return user.full_name or user.email
    return ''


def _fit_line(text, font_name, font_size, max_w):
    """Single-line fit using the font's real measured width: returns (plain_text,
    width_pt). Truncates at a word boundary with an ellipsis if the full text
    would exceed max_w — used for the header line, where every field's COLUMN
    must be exactly as wide as its own text (no more), so the gap-then-dot
    separator that follows always sits immediately after the real content
    instead of floating in whatever's left of a generous fixed-width zone."""
    text = (text or '').strip()
    if not text:
        return '', 0
    full_w = pdfmetrics.stringWidth(text, font_name, font_size)
    if full_w <= max_w:
        return text, full_w
    words = text.split()
    cur = ''
    for w in words:
        trial = f'{cur} {w}'.strip()
        if pdfmetrics.stringWidth(trial + '…', font_name, font_size) <= max_w:
            cur = trial
        else:
            break
    while cur and pdfmetrics.stringWidth(cur + '…', font_name, font_size) > max_w:
        cur = cur[:-1].rstrip()
    result = (cur or text[:1]) + '…'
    return result, pdfmetrics.stringWidth(result, font_name, font_size)


def _header_sep():
    """2-space gap, a large black dot, then a 1-space gap before the next field."""
    space_w = pdfmetrics.stringWidth(' ', 'Arimo-Bold', HEADER.fontSize)
    left_gap, right_gap = space_w * 2, space_w * 1
    w = left_gap + DOT_D + right_gap
    t = Table([[_dot()]], colWidths=[w])
    t.setStyle(TableStyle([
        ('VALIGN', (0, 0), (0, 0), 'MIDDLE'), ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('LEFTPADDING', (0, 0), (0, 0), left_gap), ('RIGHTPADDING', (0, 0), (0, 0), right_gap),
        ('TOPPADDING', (0, 0), (0, 0), 0), ('BOTTOMPADDING', (0, 0), (0, 0), 0),
    ]))
    return t, w


def _rule(width, weight=0.75):
    """A plain, solid, full-width horizontal rule (distinct from the tear
    line's dashed perforation) framing the header above and below it."""
    d = Drawing(width, weight + 1)
    d.add(Line(0, 0.5, width, 0.5, strokeColor=colors.black, strokeWidth=weight))
    return d


def _header_fields(order_no_text, buyer_name, total_count, date_text, store_sub_text):
    """Pure layout — no Order/DB access — so the exact spacing rules can be
    tuned and checked against fixed example values independent of real data.
    See _header_line for the field order and alignment rules."""
    font, size = 'Arimo-Bold', HEADER.fontSize
    cap = USABLE_W * 0.32  # generous per-field safety cap — only bites on a genuinely runaway-long value

    t1, w1 = _fit_line(order_no_text, font, size, cap)
    t2, w2 = _fit_line(buyer_name, font, size, cap)
    t4, w4 = _fit_line(date_text, font, size, cap)
    t5, w5 = _fit_line(store_sub_text, font, size, cap)

    badge = _total_badge(total_count)
    total_label_w = pdfmetrics.stringWidth('Total: ', font, size)
    zone3 = Table([[Paragraph('Total: ', HEADER), badge]], colWidths=[total_label_w, BADGE_D])
    zone3.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('ALIGN', (1, 0), (1, 0), 'LEFT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    w3 = total_label_w + BADGE_D

    sep1, sw = _header_sep()
    sep2, _ = _header_sep()
    sep3, _ = _header_sep()
    sep4, _ = _header_sep()

    used = w1 + sw + w2 + sw + w3 + sw + w4
    spacer_w = max(0, USABLE_W - used - sw - w5)

    row = [
        Paragraph(t1, HEADER), sep1, Paragraph(t2, HEADER), sep2, zone3, sep3, Paragraph(t4, HEADER),
        '', sep4, Paragraph(t5, HEADER),
    ]
    widths = [w1, sw, w2, sw, w3, sw, w4, spacer_w, sw, w5]
    header = Table([row], colWidths=widths)
    header.hAlign = 'LEFT'
    header.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))
    return [header]


def _header_line(order):
    """Plain, borderless single line, flowing left to right: each field's
    column is sized exactly to its own text, immediately followed by a
    2-space / dot / 1-space separator, then the next field (Order No ·
    Customer · Order Total [badge] · Date · Store / Sub-user). Every field is
    left-aligned EXCEPT the last (Store / Sub-user), which is pinned flush to
    the right margin — a flexible spacer soaks up whatever width is left
    between the Date field and it, so the line never trails off into empty
    space partway across the page.
    TIF/sheet numbering has no spot here yet — still pending with the print
    floor on whether sheets are named by "part" and whether a multi-page order
    needs its own page indicator; deliberately left out for now rather than
    guessed."""
    number = order.shipstation_order_number or order.external_order_number
    store = order.store.name if order.store_id else ''
    sub_user = _sub_user_name(order)
    store_line = f'{store} / {sub_user}' if sub_user else store
    # order.customer_name is often the marketplace's numeric buyer/account id
    # (confirmed on real Etsy orders), not a human name — ship_to_name is the
    # actual recipient name and is what belongs in a header meant to be read.
    buyer_name = order.ship_to_name or order.customer_name or ''
    return _header_fields(
        str(number), buyer_name,
        len(_physical_items(order)), _order_date_str(order), store_line,
    )


# ── Notes / gift message (always-reserved area under the last card row) ────
# The platform's own separate "print note" field is no longer shown here — a
# PC wanting to leave a note for the print team now uses Internal Note instead,
# same as a buyer note, just whichever one the order actually has.
_IMAGES_DIR = os.path.join(os.path.dirname(__file__), 'images')
GIFT_ICON_PATH = os.path.join(_IMAGES_DIR, 'gift_icon.png')
with Image.open(GIFT_ICON_PATH) as _gi:
    GIFT_ICON_ASPECT = _gi.width / _gi.height

NOTE_FONT_SIZE = TEXT_SMALL             # 8pt — matches the item card's small text
NOTE_LEADING = NOTE_FONT_SIZE * 1.25    # 10pt
# 1-3 item pages (big_mode) have room to spare, so Note/Gift Message try this
# larger size first and only fall back to NOTE_FONT_SIZE if it wouldn't fit
# without cutting anything — 4+ item (dense) pages always use NOTE_FONT_SIZE.
NOTE_FONT_SIZE_BIG = NOTE_FONT_SIZE + 1
NOTE_LEADING_BIG = NOTE_FONT_SIZE_BIG * 1.25
GIFT_ALONE_FONT_SIZE = TEXT_LARGE + 1   # a bit bigger when the gift message is the only thing shown
GIFT_ALONE_LEADING = GIFT_ALONE_FONT_SIZE * 1.25
MIN_NOTES_RESERVE = 3 * NOTE_LEADING    # pt — guaranteed minimum room for Note/Gift Message, every page
NOTES_TOP_GAP = 4   # pt — gap between the last card and the notes block
NOTE_GIFT_GAP = 3   # pt — gap between the note block and the gift-message block, when both are present
ICON_TOP_GAP = 3            # pt — gap between the gift message text and its icon
ICON_MAX_H = 22             # pt — icon height when a note is also present (space is tighter)
ICON_MAX_H_ALONE = 32       # pt — icon height when the gift message is the only content shown

NOTE_STYLE = ParagraphStyle('note', parent=BASE, fontSize=NOTE_FONT_SIZE, leading=NOTE_LEADING, alignment=TA_LEFT)
GIFT_STYLE = ParagraphStyle('gift', parent=BASE, fontSize=NOTE_FONT_SIZE, leading=NOTE_LEADING, alignment=TA_CENTER)
GIFT_LABEL_STYLE = ParagraphStyle('gift_label', parent=BASE, fontName='Arimo-Bold', fontSize=NOTE_FONT_SIZE,
                                   leading=NOTE_LEADING, alignment=TA_CENTER)
GIFT_ALONE_STYLE = ParagraphStyle('gift_alone', parent=BASE, fontSize=GIFT_ALONE_FONT_SIZE,
                                   leading=GIFT_ALONE_LEADING, alignment=TA_CENTER)
GIFT_ALONE_LABEL_STYLE = ParagraphStyle('gift_alone_label', parent=BASE, fontName='Arimo-Bold',
                                         fontSize=GIFT_ALONE_FONT_SIZE, leading=GIFT_ALONE_LEADING,
                                         alignment=TA_CENTER)


def _note_style(size):
    return ParagraphStyle(f'note_{size}', parent=BASE, fontSize=size, leading=size * 1.25, alignment=TA_LEFT)


def _gift_style(size):
    return ParagraphStyle(f'gift_{size}', parent=BASE, fontSize=size, leading=size * 1.25, alignment=TA_CENTER)


def _gift_label_style(size):
    return ParagraphStyle(f'gift_label_{size}', parent=BASE, fontName='Arimo-Bold', fontSize=size,
                           leading=size * 1.25, alignment=TA_CENTER)


def _wrap_line_count(text, col_w, font_name, font_size):
    """How many lines `text` needs to wrap to at col_w, with no truncation."""
    markup = _clamp(text, col_w, font_name, font_size, max_lines=100000)
    return markup.count('<br/>') + 1 if markup else 0


def _gift_icon(icon_h, max_w):
    icon_w = icon_h * GIFT_ICON_ASPECT
    if icon_w > max_w:
        icon_w, icon_h = max_w, max_w / GIFT_ICON_ASPECT
    img = RLImage(GIFT_ICON_PATH, width=icon_w, height=icon_h)
    img.hAlign = 'CENTER'
    return img


def _render_notes_block(order, max_w, max_h, prefer_larger=False):
    """Renders whatever the order has of Buyer/Internal Note and Gift Message
    into the area always reserved under the last card row. Buyer Note wins
    over Internal Note when (rarely) both exist. A gift message alone is
    shown a little larger, centered, with a small icon underneath if there's
    still room. With both a note and a gift message present, the note is
    shown first (left-aligned, as usual) and the gift message after it
    (centered) — if the two together don't fit, the gift message always
    wins in full and the note's overflow is what gets cut (Etsy caps gift
    messages at 150 characters, so this is a rare edge case in practice).

    prefer_larger (big_mode / 1-3 item pages only): try NOTE_FONT_SIZE_BIG
    first, showing everything in full at that size with no truncation; only
    if it wouldn't all fit does this fall back to the normal NOTE_FONT_SIZE
    path below (which truncates as usual)."""
    if order.buyer_note:
        note_label, note_text = 'Buyer Note:', order.buyer_note.strip()
    elif order.internal_note:
        note_label, note_text = 'Internal Note:', order.internal_note.strip()
    else:
        note_label, note_text = None, ''
    gift_text = (order.gift_message or '').strip()

    if max_h <= 0 or (not note_text and not gift_text):
        return []

    if gift_text and not note_text:
        lines_total = int(max_h // GIFT_ALONE_LEADING)
        if lines_total <= 0:
            return []
        out = []
        if lines_total >= 2:
            msg = _clamp(gift_text, max_w, 'Arimo', GIFT_ALONE_FONT_SIZE, max_lines=lines_total - 1)
            used_lines = 1 + (msg.count('<br/>') + 1)
            out.append(Paragraph('Gift Message', GIFT_ALONE_LABEL_STYLE))
            out.append(Paragraph(msg, GIFT_ALONE_STYLE))
        else:
            label_w = pdfmetrics.stringWidth('Gift Message: ', 'Arimo-Bold', GIFT_ALONE_FONT_SIZE)
            msg = _clamp(gift_text, max(10, max_w - label_w), 'Arimo', GIFT_ALONE_FONT_SIZE, max_lines=1)
            out.append(Paragraph(f'<font name="Arimo-Bold">Gift Message: </font>{msg}', GIFT_ALONE_STYLE))
            used_lines = 1
        leftover = max_h - used_lines * GIFT_ALONE_LEADING
        if leftover >= ICON_MAX_H_ALONE + ICON_TOP_GAP:
            out.append(Spacer(1, ICON_TOP_GAP))
            out.append(_gift_icon(ICON_MAX_H_ALONE, max_w))
        return out

    if note_text and not gift_text:
        if prefer_larger:
            label_w_big = pdfmetrics.stringWidth(note_label + ' ', 'Arimo-Bold', NOTE_FONT_SIZE_BIG)
            full_lines_big = _wrap_line_count(note_text, max(10, max_w - label_w_big), 'Arimo', NOTE_FONT_SIZE_BIG)
            if full_lines_big and full_lines_big * NOTE_LEADING_BIG <= max_h:
                val = _clamp(note_text, max(10, max_w - label_w_big), 'Arimo', NOTE_FONT_SIZE_BIG, max_lines=full_lines_big)
                markup = f'<font name="Arimo-Bold" size="{NOTE_FONT_SIZE_BIG}">{_txt(note_label)} </font>{val}'
                return [Paragraph(markup, _note_style(NOTE_FONT_SIZE_BIG))]
        lines_total = int(max_h // NOTE_LEADING)
        if lines_total <= 0:
            return []
        label_w = pdfmetrics.stringWidth(note_label + ' ', 'Arimo-Bold', NOTE_FONT_SIZE)
        val = _clamp(note_text, max(10, max_w - label_w), 'Arimo', NOTE_FONT_SIZE, max_lines=lines_total)
        markup = f'<font name="Arimo-Bold" size="{NOTE_FONT_SIZE}">{_txt(note_label)} </font>{val}'
        return [Paragraph(markup, NOTE_STYLE)]

    if prefer_larger:
        label_w_big = pdfmetrics.stringWidth(note_label + ' ', 'Arimo-Bold', NOTE_FONT_SIZE_BIG)
        note_full_lines_big = _wrap_line_count(note_text, max(10, max_w - label_w_big), 'Arimo', NOTE_FONT_SIZE_BIG)
        gift_lines_own_big = _wrap_line_count(gift_text, max_w, 'Arimo', NOTE_FONT_SIZE_BIG)
        needed_h = note_full_lines_big * NOTE_LEADING_BIG + NOTE_GIFT_GAP + (gift_lines_own_big + 1) * NOTE_LEADING_BIG
        if needed_h <= max_h:
            val = _clamp(note_text, max(10, max_w - label_w_big), 'Arimo', NOTE_FONT_SIZE_BIG, max_lines=note_full_lines_big)
            note_markup = f'<font name="Arimo-Bold" size="{NOTE_FONT_SIZE_BIG}">{_txt(note_label)} </font>{val}'
            gift_msg = _clamp(gift_text, max_w, 'Arimo', NOTE_FONT_SIZE_BIG, max_lines=gift_lines_own_big)
            out = [
                Paragraph(note_markup, _note_style(NOTE_FONT_SIZE_BIG)),
                Spacer(1, NOTE_GIFT_GAP),
                Paragraph('Gift Message', _gift_label_style(NOTE_FONT_SIZE_BIG)),
                Paragraph(gift_msg, _gift_style(NOTE_FONT_SIZE_BIG)),
            ]
            leftover = max_h - needed_h
            if leftover >= ICON_MAX_H + ICON_TOP_GAP:
                out.append(Spacer(1, ICON_TOP_GAP))
                out.append(_gift_icon(ICON_MAX_H, max_w))
            return out

    # Both present: the gift message always shows in full; the note gets
    # whatever lines are left over, and is dropped first if there are none.
    lines_total = max(0, int(max_h // NOTE_LEADING))
    gift_label_w = pdfmetrics.stringWidth('Gift Message: ', 'Arimo-Bold', NOTE_FONT_SIZE)
    gift_lines_own_line = _wrap_line_count(gift_text, max_w, 'Arimo', NOTE_FONT_SIZE)
    gift_lines_inline = _wrap_line_count(gift_text, max(10, max_w - gift_label_w), 'Arimo', NOTE_FONT_SIZE)

    if lines_total >= gift_lines_own_line + 1:
        gift_own_line, gift_block_lines = True, gift_lines_own_line + 1
    else:
        gift_own_line, gift_block_lines = False, gift_lines_inline

    note_lines = max(0, lines_total - gift_block_lines)

    out = []
    if note_lines > 0:
        label_w = pdfmetrics.stringWidth(note_label + ' ', 'Arimo-Bold', NOTE_FONT_SIZE)
        val = _clamp(note_text, max(10, max_w - label_w), 'Arimo', NOTE_FONT_SIZE, max_lines=note_lines)
        markup = f'<font name="Arimo-Bold" size="{NOTE_FONT_SIZE}">{_txt(note_label)} </font>{val}'
        out.append(Paragraph(markup, NOTE_STYLE))
        out.append(Spacer(1, NOTE_GIFT_GAP))

    if gift_own_line:
        gift_msg = _clamp(gift_text, max_w, 'Arimo', NOTE_FONT_SIZE, max_lines=gift_lines_own_line)
        out.append(Paragraph('Gift Message', GIFT_LABEL_STYLE))
        out.append(Paragraph(gift_msg, GIFT_STYLE))
    else:
        gift_msg = _clamp(gift_text, max(10, max_w - gift_label_w), 'Arimo', NOTE_FONT_SIZE, max_lines=gift_lines_inline)
        out.append(Paragraph(f'<font name="Arimo-Bold">Gift Message: </font>{gift_msg}', GIFT_STYLE))

    used_h = (note_lines * NOTE_LEADING + NOTE_GIFT_GAP if note_lines > 0 else 0) + gift_block_lines * NOTE_LEADING
    leftover = max_h - used_h
    if leftover >= ICON_MAX_H + ICON_TOP_GAP:
        out.append(Spacer(1, ICON_TOP_GAP))
        out.append(_gift_icon(ICON_MAX_H, max_w))

    return out


def _dense_capacity(available_for_cards, card_h, card_gap):
    """N rows cost N*card_h + (N-1)*card_gap — solved for the largest N that
    fits. Returns (rows, items_per_page) for a 2-column grid."""
    rows = max(1, int((available_for_cards + card_gap) // (card_h + card_gap)))
    return rows, rows * 2


def _paginate_order(order):
    """How many card-rows actually fit under the header is measured, not a
    hardcoded guess — CARD_H (or the header) changing size would otherwise
    silently make a fixed per-page count overflow past the frame (reportlab
    then spills the overflow onto the NEXT page's label/top frame instead of
    a proper new page — confirmed happening once CARD_H grew from 1.1" to
    1.3" while this was still a fixed constant). Returns (items, pages,
    big_mode, cards_area_h, card_h, scale) — reused by _build_order to
    actually render, and by generate_slip_pdf to count each order's pages
    ahead of time (for the "go to the next page" marker, which needs to know
    page numbers before the doc is built).

    1-3 items: single column, one full-size card per item (always fits on one
    page — MAX_BIG_ITEMS is well under what the height check below allows).
    4-6 items (however many fit the dense grid at full size): one page, same
    full-size card, 2-column grid. 7+ items: every page of this order instead
    uses the smaller SHRINK_SCALE card, so more fit per page (8 vs. 6) — pages
    are packed to that capacity (see _paginate_max_pack), not balanced, since
    the point is fitting as many items per page as that size allows."""
    items = _physical_items(order)
    header_flow = _header_line(order)
    header_core_h = sum(f.wrap(USABLE_W, BOTTOM_FRAME_H)[1] for f in header_flow)
    rule_h = _rule(USABLE_W).wrap(USABLE_W, 10)[1]
    cards_area_h = BOTTOM_FRAME_H - (rule_h + 0.5 + header_core_h + 0.5 + rule_h + HEADER_CARD_GAP)
    # MIN_NOTES_RESERVE comes off the capacity used to decide pages-per-item (not
    # off a page's actual render — see leftover_h below) so every page, dense
    # grid included, always keeps at least that much guaranteed room for notes.
    available_for_cards = cards_area_h - MIN_NOTES_RESERVE

    big_mode = len(items) <= MAX_BIG_ITEMS
    if big_mode:
        return items, [items], big_mode, cards_area_h, CARD_H, 1.0

    _, normal_capacity = _dense_capacity(available_for_cards, CARD_H, CARD_GAP)
    if len(items) <= normal_capacity:
        return items, [items], big_mode, cards_area_h, CARD_H, 1.0

    shrunk_card_h = CARD_H * SHRINK_SCALE
    shrunk_gap = CARD_GAP * SHRINK_SCALE
    _, shrunk_capacity = _dense_capacity(available_for_cards, shrunk_card_h, shrunk_gap)
    pages = _paginate_max_pack(items, max_per_page=shrunk_capacity)
    return items, pages, big_mode, cards_area_h, shrunk_card_h, SHRINK_SCALE


def _build_order(story, tif, order, label_png):
    items, pages, big_mode, cards_area_h, card_h, scale = _paginate_order(order)

    for i, page_items in enumerate(pages):
        if i > 0:
            story.append(PageBreak())
        story.append(_label_flowable(label_png, USABLE_W, TOP_FRAME_H))
        story.append(FrameBreak())

        story.append(_rule(USABLE_W))
        story.append(Spacer(1, 0.5))
        story.extend(_header_line(order))
        story.append(Spacer(1, 0.5))
        story.append(_rule(USABLE_W))
        story.append(Spacer(1, HEADER_CARD_GAP))

        is_last_page = i == len(pages) - 1

        if big_mode:
            story.extend(_item_rows_big(page_items, USABLE_W))
            n_rows = len(page_items)
        else:
            story.extend(_item_rows_dense(page_items, USABLE_W, card_h=card_h, scale=scale))
            n_rows = -(-len(page_items) // 2)

        if is_last_page:
            card_gap = CARD_GAP * scale
            cards_used_h = n_rows * card_h + max(0, n_rows - 1) * card_gap
            leftover_h = cards_area_h - cards_used_h - NOTES_TOP_GAP
            notes_block = _render_notes_block(order, USABLE_W, leftover_h, prefer_larger=big_mode)
            if notes_block:
                story.append(Spacer(1, NOTES_TOP_GAP))
                story.extend(notes_block)


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
    return total


NEXT_PAGE_ARROW_PATH = os.path.join(_IMAGES_DIR, 'next_page_arrow.png')
with Image.open(NEXT_PAGE_ARROW_PATH) as _ap:
    NEXT_PAGE_ARROW_ASPECT = _ap.width / _ap.height  # width:height
CONTINUE_TEXT = 'Go to the next page'
CONTINUE_FONT_SIZE = 7
CONTINUE_ARROW_W = MARGIN - 10   # pt — sized to whatever's left of the right margin strip, grid untouched
CONTINUE_GAP = 4                 # pt — gap between the rotated text and the arrow below it


def _draw_tear_line(canvas_obj, doc):
    """The label stock's own perforation, at the physical middle of the page, plus
    (when this page isn't an order's last) a small "go to the next page" marker
    in the right margin — real rotated text, not baked into the arrow image, so
    it still lives in the PDF's text layer. Which pages get the marker is decided
    up front in generate_slip_pdf (doc._arrow_pages), since by the time any page
    is actually being drawn here the page count for later orders isn't known yet."""
    canvas_obj.saveState()
    canvas_obj.setDash(4, 3)
    canvas_obj.setLineWidth(0.75)
    canvas_obj.setStrokeColor(colors.grey)
    canvas_obj.line(0, HALF_H, PAGE_W, HALF_H)
    canvas_obj.restoreState()

    if canvas_obj.getPageNumber() not in getattr(doc, '_arrow_pages', ()):
        return

    canvas_obj.saveState()
    text_len = pdfmetrics.stringWidth(CONTINUE_TEXT, 'Arimo-Bold', CONTINUE_FONT_SIZE)
    arrow_h = CONTINUE_ARROW_W / NEXT_PAGE_ARROW_ASPECT
    total_h = text_len + CONTINUE_GAP + arrow_h
    cx = PAGE_W - MARGIN / 2
    cy = MARGIN + BOTTOM_FRAME_H / 2

    canvas_obj.translate(cx, cy + total_h / 2)
    canvas_obj.rotate(-90)
    canvas_obj.setFont('Arimo-Bold', CONTINUE_FONT_SIZE)
    canvas_obj.drawString(0, -CONTINUE_FONT_SIZE * 0.35, CONTINUE_TEXT)
    canvas_obj.restoreState()

    arrow_y = cy - total_h / 2
    canvas_obj.drawImage(NEXT_PAGE_ARROW_PATH, cx - CONTINUE_ARROW_W / 2, arrow_y,
                          width=CONTINUE_ARROW_W, height=arrow_h)


def _build_doc(buf, title):
    top_frame = Frame(MARGIN, HALF_H + TEAR_GAP, USABLE_W, TOP_FRAME_H, id='top',
                       leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    bottom_frame = Frame(MARGIN, MARGIN, USABLE_W, BOTTOM_FRAME_H, id='bottom',
                          leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    preview_frame = Frame(MARGIN, MARGIN, USABLE_W, PAGE_H - 2 * MARGIN, id='preview',
                           leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc = BaseDocTemplate(buf, pagesize=letter, title=title)
    doc.addPageTemplates([
        PageTemplate(id='slip', frames=[top_frame, bottom_frame], onPage=_draw_tear_line),
        PageTemplate(id='preview', frames=[preview_frame]),
    ])
    return doc


def generate_slip_pdf(
    orders: List,                           # order objects in the order they appear on the sheet
    sheet_number: int,                      # the gang sheet's number — printed as TIF_<n> on every page
    label_images: Optional[dict] = None,    # {order_id: PNG bytes}; falls back to the stored label file
    preview=None,                           # PIL image of the sheet with order numbers (first page(s))
) -> bytes:
    """Slip PDF for ONE gang sheet: optional sheet preview page(s), then one or more
    pages per order (more only when it has over MAX_ITEMS_PER_PAGE items). Every page
    carries the sheet's TIF number, exactly as the film file is numbered, so film and
    paperwork stay paired."""
    from apps.shipping.labels import load_label_png

    _thumb_raw_cache.clear()
    _item_photo_cache.clear()
    buf = io.BytesIO()
    doc = _build_doc(buf, title=f'TIF_{sheet_number} order slips')

    story = [NextPageTemplate('preview' if preview is not None else 'slip')]
    running_page = 1
    if preview is not None:
        running_page += _preview_pages(story, preview, sheet_number, USABLE_W)
        story.append(NextPageTemplate('slip'))

    # Page numbers needing the "go to the next page" marker are decided here,
    # up front — by the time _draw_tear_line actually draws a given page, later
    # orders' own page counts (which don't affect THIS page) aren't relevant,
    # but THIS order's own remaining page count needs to already be known, and
    # the onPage callback only sees one page at a time.
    arrow_pages = set()
    for order in orders:
        n_pages = len(_paginate_order(order)[1])
        if n_pages > 1:
            arrow_pages.update(range(running_page, running_page + n_pages - 1))
        running_page += n_pages
    doc._arrow_pages = arrow_pages

    for order in orders:
        label_png = (label_images or {}).get(order.id) or load_label_png(order)
        _build_order(story, sheet_number, order, label_png)
        story.append(PageBreak())

    doc.build(story)
    return buf.getvalue()
