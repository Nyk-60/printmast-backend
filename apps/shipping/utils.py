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


def map_address_status(shipstation_value):
    """ShipStation shipTo.addressVerified text -> Order.AddressStatus value."""
    text = (shipstation_value or '').lower()
    if 'validated successfully' in text:
        return 'validated'
    if 'warning' in text:
        return 'warning'
    if 'failed' in text:
        return 'failed'
    return 'unknown'  # "not yet validated", "not supported", missing


def sanitize_label_text(value):
    """Make free text safe for carrier/label APIs: strip emoji, symbols and control
    characters, fold accents to ASCII, collapse whitespace. Marketplace names and
    addresses routinely contain emoji that ShipStation tolerates but stricter label
    APIs reject. Returns '' if nothing printable survives (caller must treat that
    as a missing field rather than send garbage)."""
    import re
    import unicodedata

    text = unicodedata.normalize('NFKD', value or '')
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r'[^\x20-\x7E]', '', text)          # printable ASCII only
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# Reseller/account carrier codes that are really the same underlying carrier — e.g.
# ShipStation quotes USPS through its 'stamps_com' integration while ByeLabel quotes
# the identical USPS product through its own 'usps' account. To the sub-user these are
# not two different shipping choices, just two accounts buying the same real service.
_CARRIER_ALIASES = {
    'stamps_com': 'usps', 'usps': 'usps', 'endicia': 'usps',
    'ups': 'ups', 'ups_walleted': 'ups',
    'fedex': 'fedex', 'fedex_walleted': 'fedex',
    'dhl_express': 'dhl',
}


def real_service_key(carrier_code, service_code):
    """Collapse a (carrier_code, service_code) pair to the real-world service it
    represents, independent of which provider/account quoted it.

    Used to dedupe rate quotes shown to the sub-user: ShipStation's stamps_com
    account and ByeLabel's own USPS account can both quote "USPS Ground
    Advantage" at different prices — the sub-user should see that ONE product
    once (whichever account is cheaper right now), not two near-identical,
    confusingly priced lines. Different real carriers/services (USPS vs UPS)
    still return different keys and are never merged.
    """
    carrier = _CARRIER_ALIASES.get((carrier_code or '').lower(), (carrier_code or '').lower())
    service = ''.join(ch for ch in (service_code or '').lower() if ch.isalnum())
    return (carrier, service)


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


def get_fixed_shipping_price(print_center, category, weight_oz):
    """Look up the print center's flat shipping price for this category/weight.

    Returns a Decimal, or None if no bracket covers this weight (caller must
    treat this the same as "no rate available" — never silently fall back to
    a wrong bracket).
    """
    from decimal import Decimal

    from django.db.models import Q

    from .models import FixedShippingRate

    weight = Decimal(str(weight_oz))
    qs = FixedShippingRate.objects.filter(
        print_center=print_center, category=category, min_weight_oz__lte=weight,
    ).filter(Q(max_weight_oz__isnull=True) | Q(max_weight_oz__gte=weight))
    bracket = qs.order_by('min_weight_oz').first()
    return bracket.price if bracket else None


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
