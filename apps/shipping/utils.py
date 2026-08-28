"""Shipping utility functions — dimension calculation for orders."""


_PLATFORM_DEFAULT = {'length': 12.0, 'width': 10.0, 'height': 2.0, 'units': 'inches'}


def _get_item_dims(variant, quantity):
    """Return dimension dict for one order item.

    Priority:
      1. Explicit VariantDimensionRule matching the quantity range
      2. Formula: H = dim_height_base_in + (qty-1) * dim_height_per_unit_in
      3. Returns {} if no dims configured on the variant
    """
    # Check explicit quantity rules (already ordered by qty_min)
    for rule in variant.dimension_rules.all():
        if rule.qty_min <= quantity and (rule.qty_max is None or quantity <= rule.qty_max):
            return {
                'length':      float(rule.length_in),
                'width':       float(rule.width_in),
                'height':      float(rule.height_in),
                'is_stackable': variant.is_stackable,
            }

    # Fall back to formula
    L = float(variant.dim_length_in or 0)
    W = float(variant.dim_width_in or 0)
    H_base = float(variant.dim_height_base_in or 0)
    H_per  = float(variant.dim_height_per_unit_in or 0)
    step   = int(variant.dim_height_step or 1)

    if not (L > 0 and W > 0):
        return {}

    if H_per > 0:
        # H increases by H_per every `step` items
        # e.g. step=5, per=0.5: qty 1-5 → base, qty 6-10 → base+0.5, qty 11+ → base+1.0
        H = H_base + ((quantity - 1) // step) * H_per
    elif variant.is_stackable:
        H = H_base * quantity
    else:
        H = H_base  # non-stackable: height doesn't change with quantity

    return {
        'length':      L,
        'width':       W,
        'height':      max(float(H), 0.25),
        'is_stackable': variant.is_stackable,
    }


def calculate_order_dimensions(order):
    """Calculate package dimensions for a matched order.

    Combination strategy:
      - L = max of all item lengths
      - W = max of all item widths
      - H = sum of stackable item heights + max of non-stackable item heights

    Returns a dict compatible with ShipStation's dimensions payload.
    Falls back to platform default (12×10×2) if no dims are configured.
    """
    items = [
        i for i in order.items.all()
        if i.pc_variant_id and float(i.unit_price) >= 0
    ]
    if not items:
        return _PLATFORM_DEFAULT.copy()

    item_dims = []
    for item in items:
        d = _get_item_dims(item.pc_variant, item.quantity)
        if d:
            item_dims.append(d)

    if not item_dims:
        return _PLATFORM_DEFAULT.copy()

    L = max(d['length'] for d in item_dims)
    W = max(d['width']  for d in item_dims)

    stackable     = [d for d in item_dims if     d.get('is_stackable', True)]
    non_stackable = [d for d in item_dims if not d.get('is_stackable', True)]

    H_stack = sum(d['height'] for d in stackable)     if stackable     else 0
    H_rigid = max(d['height'] for d in non_stackable) if non_stackable else 0
    H = H_stack + H_rigid

    return {
        'length': round(L, 2),
        'width':  round(W, 2),
        'height': round(max(H, 0.25), 2),
        'units':  'inches',
    }


def categorize_requested_service(requested_service):
    """Map a marketplace requested_service string to a ShippingServiceOption category.

    Falls back to 'standard' when no keyword matches.
    """
    s = (requested_service or '').lower()
    if 'express' in s or 'overnight' in s or 'next day' in s:
        return 'express'
    if 'priority' in s or 'expedited' in s or '2nd day' in s or '2-day' in s or 'second day' in s:
        return 'priority'
    return 'standard'


def get_order_shipping_config(order):
    """Return (weight_oz, dimensions) for an order, honoring shipping_override.

    Used both for the post-match push and to refresh ShipStation right
    before a label purchase, so weight/dims/shipFrom are always current.
    """
    override = order.shipping_override or {}
    if override:
        weight_oz = override.get('weight_oz')
        length = override.get('length')
        dimensions = (
            {'length': length, 'width': override['width'], 'height': override['height'], 'units': 'inches'}
            if length else None
        )
    else:
        billable = [i for i in order.items.all() if float(i.unit_price) >= 0 and i.pc_variant_id]
        weight_oz = sum(float(i.pc_variant.weight_oz or 0) * i.quantity for i in billable) or None
        dimensions = calculate_order_dimensions(order)
    return weight_oz, dimensions
