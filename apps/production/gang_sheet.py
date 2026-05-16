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
        if file_obj:
            try:
                file_obj.seek(0)
                design_img = Image.open(file_obj).convert('RGBA')
                if p.rotated:
                    design_img = design_img.rotate(90, expand=True)
                design_img = design_img.resize((w_px, h_px), Image.LANCZOS)
                canvas.paste(design_img, (x_px, y_px), design_img)
            except Exception:
                # Draw placeholder if image fails to load
                draw.rectangle([x_px + 2, y_px + 2, x_px + w_px - 2, y_px + h_px - 2],
                               fill=(230, 230, 230))

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
