"""PDF export for OrderViewSet.product_breakdown — a "Supply List": what's
actually in the In Production (or other) bucket right now, grouped by
catalog product (colored header bar + size/color/qty table underneath),
highest-quantity product first. Whether a given unit is already in the PC's
own stock vs. needs same-day sourcing from a supplier isn't tracked anywhere
— the PC decides that themselves from this list.

Portrait, two newspaper-style columns divided by a vertical rule, content
flowing left-column-then-right-column-then-next-page automatically (reportlab
Frame mechanics) — not a manual half/half split, so a column running long
never desyncs anything (each column is its own independent top-to-bottom flow).
"""
import io

import pytz
from django.utils import timezone as django_tz
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, Frame, KeepTogether, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)

# Registers the Arimo font family (side effect of import) — the same house
# font used for the production order-slip PDF, kept consistent here too.
import apps.production.slip_pdf  # noqa: F401

CHICAGO = pytz.timezone('America/Chicago')

GROUP_STYLE = ParagraphStyle('bd_group', fontName='Arimo-Bold', fontSize=8.5, leading=11, textColor=colors.white)
TITLE_STYLE = ParagraphStyle('bd_title', fontName='Arimo-Bold', fontSize=8.5, leading=11)
META_STYLE  = ParagraphStyle('bd_meta', fontName='Arimo', fontSize=7, leading=10, textColor=colors.grey)
EMPTY_STYLE = ParagraphStyle('bd_empty', fontName='Arimo', fontSize=8, leading=11, textColor=colors.grey)

PAGE_W, PAGE_H = A4
MARGIN = 0.25 * inch
GUTTER = 0.2 * inch
COL_WIDTH = (PAGE_W - 2 * MARGIN - GUTTER) / 2

NAME_COL_WIDTH  = COL_WIDTH * 0.72
TOTAL_COL_WIDTH = COL_WIDTH * 0.28
VARIANT_COL_WIDTHS = [COL_WIDTH * 0.48, COL_WIDTH * 0.27, COL_WIDTH * 0.25]
VARIANT_FONTSIZE = 6.5


def _draw_divider(canvas, doc):
    canvas.saveState()
    x = PAGE_W / 2
    canvas.setStrokeColor(colors.HexColor('#cccccc'))
    canvas.setLineWidth(0.5)
    canvas.line(x, MARGIN * 0.4, x, PAGE_H - MARGIN * 0.4)
    canvas.restoreState()


def generate_breakdown_pdf(products, tab_label, filter_label=''):
    buf = io.BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=A4,
        topMargin=MARGIN, bottomMargin=MARGIN, leftMargin=MARGIN, rightMargin=MARGIN,
    )
    col_height = PAGE_H - 2 * MARGIN
    left_frame  = Frame(MARGIN, MARGIN, COL_WIDTH, col_height, id='left',
                         leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    right_frame = Frame(MARGIN + COL_WIDTH + GUTTER, MARGIN, COL_WIDTH, col_height, id='right',
                         leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([
        PageTemplate(id='TwoCol', frames=[left_frame, right_frame], onPage=_draw_divider),
    ])

    created_at = django_tz.now().astimezone(CHICAGO).strftime('%b %d, %Y %I:%M %p %Z')
    meta = f'Created at: {created_at}'
    if filter_label:
        meta += f'  ·  {filter_label}'

    story = [
        Paragraph(f'Supply List — {tab_label}', TITLE_STYLE),
        Paragraph(meta, META_STYLE),
        Spacer(1, 10),
    ]

    if not products:
        story.append(Paragraph('Nothing in this status right now.', EMPTY_STYLE))
        doc.build(story)
        return buf.getvalue()

    for p in products:
        header = Table(
            [[Paragraph(p['product_name'], GROUP_STYLE),
              Paragraph(f"Total: {p['total_quantity']}", GROUP_STYLE)]],
            colWidths=[NAME_COL_WIDTH, TOTAL_COL_WIDTH],
        )
        header.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#2d9e93')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (0, 0), 5),
            ('RIGHTPADDING', (-1, 0), (-1, 0), 5),
            ('TOPPADDING', (0, 0), (-1, -1), 1.5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 1.5),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ]))

        rows = [['Color', 'Size', 'Qty']] + [
            [v['color'] or '—', v['size'] or '—', str(v['quantity'])] for v in p['variants']
        ]
        # Fixed, deliberately tight row height instead of letting the table
        # auto-size from the font's own ascent/descent metrics — that auto
        # height reserves descender space most of these words/numbers never
        # use (Black, White, M, 9…), which is what made rows look bottom-
        # heavy no matter how padding/valign were tuned.
        row_h = VARIANT_FONTSIZE + 5.5
        table = Table(rows, colWidths=VARIANT_COL_WIDTHS, rowHeights=[row_h] * len(rows))
        table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, 0), 'Arimo-Bold'),
            ('FONTNAME', (0, 1), (-1, -1), 'Arimo'),
            ('FONTSIZE', (0, 0), (-1, -1), VARIANT_FONTSIZE),
            ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
            ('LINEBELOW', (0, 0), (-1, 0), 0.5, colors.HexColor('#d0d0d0')),
            ('LINEBELOW', (0, 1), (-1, -2), 0.25, colors.HexColor('#eeeeee')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            # Even with a fixed row height, MIDDLE still centers by the font's
            # ascent/descent box, not visible ink — pushing a bit more padding
            # off the top than the bottom nudges the actual glyphs down into
            # true visual center.
            ('TOPPADDING', (0, 0), (-1, -1), 2.2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        # Keep each product's header bar + its own rows from splitting across
        # a column/page break — a colored header on one column with its data
        # orphaned onto the next, header-less, is worse than just moving the
        # whole group down to start the next column.
        story.append(KeepTogether([header, table]))
        story.append(Spacer(1, 8))

    doc.build(story)
    return buf.getvalue()
