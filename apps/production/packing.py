"""
2D strip packing for gang sheet layout.

Rules:
- Strip width is fixed (22 inches).
- Designs in the same order must be placed close together (same shelf if possible).
- Each design can be rotated 0° or 90°.
- Objective: minimize total strip height (= minimize waste).

Algorithm: Shelf Best Fit with group affinity.
  1. Build OrderGroup list (one per order).
  2. For each group, choose optimal rotation for each design (minimize group shelf height).
  3. Sort groups by height descending (tallest first → fewer gaps).
  4. Assign each group to the first shelf where it fits; open a new shelf if none.
  5. Within a group, designs are laid out left-to-right.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class DesignPlacement:
    design_id: int          # OrderItemDesign pk
    order_id: int
    x_in: float             # left edge (inches from sheet left)
    y_in: float             # top edge (inches from sheet top)
    w_in: float             # placed width (may be rotated)
    h_in: float             # placed height (may be rotated)
    rotated: bool = False   # True = 90° CW rotation applied
    group_seq: int = 0      # sequence number shared by all designs of same order


@dataclass
class _Design:
    design_id: int
    order_id: int
    orig_w: float   # physical width in inches (from print area)
    orig_h: float   # physical height in inches (from print area)


@dataclass
class _Shelf:
    y: float        # top edge
    height: float   # current shelf height
    x_cursor: float # next available x position


def pack(
    designs: List[Tuple[int, int, float, float]],
    sheet_width: float = 22.0,
    margin: float = 0.20,
    max_sheet_length: float = 200.0,
) -> Tuple[List[List[DesignPlacement]], float]:
    """
    Pack designs onto gang sheet(s).

    Args:
        designs: list of (design_id, order_id, width_inches, height_inches)
        sheet_width: fixed sheet width in inches (default 22")
        margin: cut margin between designs in inches
        max_sheet_length: maximum sheet length before starting a new sheet

    Returns:
        (sheets, used_length_per_sheet)
        sheets: list of sheets; each sheet is list of DesignPlacement
        Each sheet's max y + last design height gives the used length.
    """
    if not designs:
        return [], 0.0

    # Build design objects
    raw = [_Design(d[0], d[1], d[2], d[3]) for d in designs]

    # Group by order
    from collections import defaultdict
    groups_map: dict = defaultdict(list)
    for d in raw:
        groups_map[d.order_id].append(d)

    # For each design choose rotation that minimises height
    for group in groups_map.values():
        for d in group:
            # If 90° rotation makes it shorter (fits better in shelf), apply it
            if d.orig_h > d.orig_w:
                # rotating makes it wider but shorter
                d.orig_w, d.orig_h = d.orig_h, d.orig_w
                d._rotated = True
            else:
                d._rotated = False

    # Sort groups: tallest design height first → better shelf utilisation
    sorted_groups = sorted(
        groups_map.items(),
        key=lambda kv: max(d.orig_h for d in kv[1]),
        reverse=True,
    )

    all_sheets: List[List[DesignPlacement]] = []
    current_sheet: List[DesignPlacement] = []
    shelves: List[_Shelf] = []
    seq = 1
    order_seq_map: dict = {}  # order_id → assigned group_seq (consistent across rows)

    def _current_sheet_height():
        if not shelves:
            return 0.0
        last = shelves[-1]
        return last.y + last.height

    def _group_fits_on_shelf(shelf: _Shelf, group_w: float) -> bool:
        return shelf.x_cursor + group_w <= sheet_width + 1e-9

    def _start_new_sheet():
        nonlocal current_sheet, shelves
        if current_sheet:
            all_sheets.append(current_sheet)
        current_sheet = []
        shelves = []

    def _place_group(order_id, group_designs, shelf: _Shelf):
        nonlocal seq
        if order_id not in order_seq_map:
            order_seq_map[order_id] = seq
            seq += 1
        group_seq = order_seq_map[order_id]
        x = shelf.x_cursor
        for d in group_designs:
            placement = DesignPlacement(
                design_id=d.design_id,
                order_id=order_id,
                x_in=x,
                y_in=shelf.y,
                w_in=d.orig_w,
                h_in=d.orig_h,
                rotated=getattr(d, '_rotated', False),
                group_seq=group_seq,
            )
            current_sheet.append(placement)
            x += d.orig_w + margin

        # Update shelf
        group_w = sum(d.orig_w for d in group_designs) + margin * (len(group_designs) - 1)
        group_h = max(d.orig_h for d in group_designs)
        shelf.x_cursor += group_w + margin
        shelf.height = max(shelf.height, group_h)

    for order_id, group in sorted_groups:
        group_w = (
            sum(d.orig_w for d in group)
            + margin * (len(group) - 1)
        )
        group_h = max(d.orig_h for d in group)

        # If group is wider than the sheet, split it across rows (each design gets its own row)
        if group_w > sheet_width:
            # Fall back: place each design individually (no affinity)
            for d in group:
                _place_single(d, order_id, shelves, current_sheet, sheet_width,
                               margin, max_sheet_length, seq,
                               _start_new_sheet, _current_sheet_height)
                seq += 1
            continue

        # Check if adding this group would overflow the current sheet
        if shelves and _current_sheet_height() + group_h + margin > max_sheet_length:
            _start_new_sheet()

        # Find a shelf where this group fits
        placed = False
        for shelf in reversed(shelves):  # try most recent first
            if _group_fits_on_shelf(shelf, group_w):
                _place_group(order_id, group, shelf)
                placed = True
                break

        if not placed:
            # Open a new shelf
            y = _current_sheet_height() + (margin if shelves else 0)
            new_shelf = _Shelf(y=y, height=0.0, x_cursor=0.0)
            shelves.append(new_shelf)
            _place_group(order_id, group, new_shelf)

    if current_sheet:
        all_sheets.append(current_sheet)

    # Post-process: ensure all designs from the same order share one group_seq
    # (order may have been split across rows when it exceeded sheet width)
    final_seq = 1
    order_final_seq: dict = {}
    for sheet in all_sheets:
        for p in sheet:
            if p.order_id not in order_final_seq:
                order_final_seq[p.order_id] = final_seq
                final_seq += 1
            p.group_seq = order_final_seq[p.order_id]

    return all_sheets


def _place_single(d, order_id, shelves, current_sheet, sheet_width,
                   margin, max_sheet_length, seq, start_new_sheet_fn, height_fn):
    """Fallback: place one design on its own row if group is too wide."""
    placement = DesignPlacement(
        design_id=d.design_id,
        order_id=order_id,
        x_in=0.0,
        y_in=height_fn() + (margin if shelves else 0),
        w_in=d.orig_w,
        h_in=d.orig_h,
        rotated=getattr(d, '_rotated', False),
        group_seq=seq,
    )
    current_sheet.append(placement)
    shelf = type('S', (), {'y': placement.y_in, 'height': d.orig_h, 'x_cursor': d.orig_w + margin})()
    shelves.append(shelf)


def total_length(placements: List[DesignPlacement], margin: float = 0.20) -> float:
    """Return the total used length of a sheet's placements."""
    if not placements:
        return 0.0
    return max(p.y_in + p.h_in for p in placements) + margin
