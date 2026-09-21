"""
Gang sheet PNG renderer using Pillow.

Each design is rendered at its physical print-area size (inches × DPI).
Designs from the same order get a sequence-number label in the corner.
Margin lines are drawn as thin dashed guides to aid cutting.
"""
from __future__ import annotations

import io
import math
from typing import List

from PIL import Image, ImageDraw, ImageFont

from .packing import DesignPlacement


class DesignRenderError(Exception):
    """A design could not be placed on the sheet. Failing loudly is deliberate: the
    old behaviour (a grey placeholder box) would have printed a blank rectangle on
    film and silently shipped the wrong product."""


SHEET_WIDTH_IN = 22.0
GUIDE_COLOR = (180, 180, 180)   # light grey cut guides
LABEL_BG = (255, 220, 0)        # yellow group label
LABEL_FG = (0, 0, 0)


def render_gang_sheet(
    placements: List[DesignPlacement],
    design_files: dict,           # {design_id: file-like or path}
    sheet_length_in: float,
    dpi: int = 300,
    margin_in: float = 0.20,
) -> bytes:
    """
    Render one gang sheet and return PNG bytes.

    design_files: mapping from design_id → opened file-like object (seek(0) before use)
    """
    width_px = int(SHEET_WIDTH_IN * dpi)
    length_px = int(sheet_length_in * dpi)
    margin_px = int(margin_in * dpi)

    canvas = Image.new('RGBA', (width_px, length_px), (255, 255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    # Try to load a font for sequence labels; fall back to default
    try:
        font = ImageFont.truetype("arial.ttf", size=max(24, dpi // 10))
        small_font = ImageFont.truetype("arial.ttf", size=max(16, dpi // 16))
    except Exception:
        font = ImageFont.load_default()
        small_font = font

    for p in placements:
        x_px = int(p.x_in * dpi)
        y_px = int(p.y_in * dpi)
        w_px = int(p.w_in * dpi)
        h_px = int(p.h_in * dpi)

        # Draw cut guide rectangle
        draw.rectangle([x_px, y_px, x_px + w_px, y_px + h_px],
                       outline=GUIDE_COLOR, width=max(1, dpi // 150))

        # Place design image
        file_obj = design_files.get(p.design_id)
        if not file_obj:
            raise DesignRenderError(f'design {p.design_id} (order {p.order_id}): file is missing')
        try:
            file_obj.seek(0)
            design_img = Image.open(file_obj).convert('RGBA')
            if p.rotated:
                design_img = design_img.rotate(90, expand=True)
            design_img = design_img.resize((w_px, h_px), Image.LANCZOS)
            canvas.paste(design_img, (x_px, y_px), design_img)
        except Exception as exc:
            raise DesignRenderError(f'design {p.design_id} (order {p.order_id}): {exc}') from exc

        # Sequence number label (top-left corner of design)
        label = str(p.group_seq)
        label_pad = max(4, dpi // 75)
        lw = max(30, dpi // 8)
        lh = max(20, dpi // 12)
        draw.rectangle([x_px, y_px, x_px + lw, y_px + lh], fill=LABEL_BG)
        draw.text((x_px + label_pad, y_px + label_pad // 2), label,
                  fill=LABEL_FG, font=font)

        # Dashed margin guides (right and bottom edges)
        _draw_dashed_line(draw, x_px + w_px, y_px, x_px + w_px, y_px + h_px,
                          dash=max(8, dpi // 30), color=GUIDE_COLOR)
        _draw_dashed_line(draw, x_px, y_px + h_px, x_px + w_px, y_px + h_px,
                          dash=max(8, dpi // 30), color=GUIDE_COLOR)

    buf = io.BytesIO()
    canvas.convert('RGB').save(buf, format='PNG', optimize=False, compress_level=1)
    buf.seek(0)
    return buf.read()


def _draw_dashed_line(draw, x1, y1, x2, y2, dash=10, color=(180, 180, 180)):
    """Draw a dashed line between two points."""
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        return
    steps = int(length / dash)
    for i in range(0, steps, 2):
        t0, t1 = i / steps, min((i + 1) / steps, 1)
        draw.line([
            (x1 + dx * t0, y1 + dy * t0),
            (x1 + dx * t1, y1 + dy * t1),
        ], fill=color, width=1)


# ── Preview (page 1 of the slip PDF) ─────────────────────────────────────────

_PALETTE = [
    (238, 130, 238), (0, 0, 255), (0, 255, 255), (255, 140, 0), (0, 170, 0),
    (220, 20, 60), (128, 0, 128), (0, 128, 128), (184, 134, 11), (70, 130, 180),
]


def _preview_font(size):
    import os

    import reportlab
    path = os.path.join(os.path.dirname(reportlab.__file__), 'fonts', 'VeraBd.ttf')
    try:
        return ImageFont.truetype(path, size=size)
    except Exception:
        return ImageFont.load_default()


def render_sheet_preview(
    placements: List[DesignPlacement],
    design_files: dict,
    order_labels: dict,           # {order_id: "4163893199"}
    sheet_length_in: float,
    width_px: int = 1100,
) -> Image.Image:
    """A small picture of the sheet for the person at the press: each design keeps
    its place, gets a coloured frame per order and the order number on top, so film
    can be matched to slips at a glance. Order numbers exist only here, never on the
    film itself. Drawn straight from the placements (not by shrinking the huge sheet)."""
    scale = width_px / SHEET_WIDTH_IN                        # preview px per inch
    height_px = max(1, int(sheet_length_in * scale))
    canvas = Image.new('RGB', (width_px, height_px), (224, 224, 224))
    draw = ImageDraw.Draw(canvas)
    font = _preview_font(max(14, int(width_px / 42)))

    colors = {}
    for p in placements:
        colors.setdefault(p.order_id, _PALETTE[len(colors) % len(_PALETTE)])

    for p in placements:
        x, y = int(p.x_in * scale), int(p.y_in * scale)
        w, h = max(2, int(p.w_in * scale)), max(2, int(p.h_in * scale))
        file_obj = design_files.get(p.design_id)
        if file_obj:
            try:
                file_obj.seek(0)
                art = Image.open(file_obj).convert('RGBA')
                if p.rotated:
                    art = art.rotate(90, expand=True)
                art = art.resize((w, h), Image.LANCZOS)
                canvas.paste(art, (x, y), art)
            except Exception:
                pass
        color = colors[p.order_id]
        draw.rectangle([x, y, x + w, y + h], outline=color, width=4)

        text = str(order_labels.get(p.order_id, p.order_id))
        box = draw.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        if p.rotated:
            tag = Image.new('RGBA', (tw + 12, th + 10), (255, 255, 255, 235))
            ImageDraw.Draw(tag).text((6, 3 - box[1]), text, fill=(0, 0, 0), font=font)
            tag = tag.rotate(90, expand=True)
            canvas.paste(tag, (x + 4, y + 4), tag)
        else:
            draw.rectangle([x + 4, y + 4, x + 4 + tw + 12, y + 4 + th + 10], fill=(255, 255, 255))
            draw.text((x + 10, y + 7 - box[1]), text, fill=(0, 0, 0), font=font)
    return canvas
