"""Order processing service functions."""
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

_CENTS = Decimal('0.01')


def log_event(order, event_type, actor=None, description='', metadata=None):
    """Append one audit-trail entry for an order. Never raises — a logging
    failure must never break the operation it's describing."""
    from .models import OrderEvent
    try:
        OrderEvent.objects.create(
            order=order, event_type=event_type, actor=actor,
            description=description, metadata=metadata,
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Failed to log OrderEvent %s for order %s', event_type, order.pk)


def billable_items(order):
    """Order items that are matched and not a discount/adjustment line."""
    return [i for i in order.items.all() if i.pc_variant_id and float(i.unit_price) >= 0]


def calculate_order_cost(order):
    """Single source of truth for what an order actually costs the sub-user.

    Used both to display the cost breakdown and to charge the sub-user's
    balance at send-to-print — the two must never drift apart, or the print
    center ends up producing at less than cost (or for free).

    Returns Decimal values: (item_cost, extra_print_area_fee, shipping_cost, total).
    """
    items = billable_items(order)

    item_cost = sum((i.pc_variant.cost * i.quantity for i in items), Decimal('0'))
    item_cost = item_cost.quantize(_CENTS, rounding=ROUND_HALF_UP)

    extra_print_area_fee = Decimal('0')
    sub_user = order.sub_user
    if sub_user and sub_user.extra_print_area_enabled:
        price = sub_user.extra_print_area_price or Decimal('0')
        if price > 0:
            extra_units = sum(max(0, i.designs.count() - 1) * i.quantity for i in items)
            extra_print_area_fee = (extra_units * price).quantize(_CENTS, rounding=ROUND_HALF_UP)

    shipping_cost = order.shipping_cost or Decimal('0')
    total = (item_cost + extra_print_area_fee + shipping_cost).quantize(_CENTS, rounding=ROUND_HALF_UP)
    return item_cost, extra_print_area_fee, shipping_cost, total


def refund_order_debit(order, actor, description):
    """Refund whatever net amount is currently charged for this order, if any.

    Nets all past DEBIT transactions against this order against all past REFUNDs
    for it, so repeated send-to-print / recall cycles always settle correctly
    without over- or under-refunding. No-op (returns None) if nothing is owed
    back. Locks and updates the sub_user's balance — call within a transaction.
    """
    from django.db.models import Sum

    from apps.accounts.models import BalanceTransaction

    sub_user = type(order.sub_user).objects.select_for_update().get(pk=order.sub_user_id)

    debited = BalanceTransaction.objects.filter(
        order=order, type=BalanceTransaction.Type.DEBIT
    ).aggregate(s=Sum('amount'))['s'] or Decimal('0')
    refunded = BalanceTransaction.objects.filter(
        order=order, type=BalanceTransaction.Type.REFUND
    ).aggregate(s=Sum('amount'))['s'] or Decimal('0')
    outstanding = debited - refunded
    if outstanding <= 0:
        return None

    sub_user.balance += outstanding
    sub_user.save(update_fields=['balance'])
    return BalanceTransaction.objects.create(
        sub_user=sub_user,
        type=BalanceTransaction.Type.REFUND,
        amount=outstanding,
        order=order,
        order_reference=order.shipstation_order_id,
        description=description,
        created_by=actor,
    )


def _variant_key(raw_options: dict) -> str:
    """Canonical string key from raw_options dict for mapping lookup."""
    return "|".join(f"{k}:{v}" for k, v in sorted(raw_options.items()))


def auto_match_item(order_item, actor=None) -> bool:
    """
    Try to match an OrderItem to a PrintCenterVariant using saved ProductMappings.
    Returns True if matched.
    """
    from .models import ProductMapping

    sub_user = order_item.order.sub_user
    store = order_item.order.store
    sku = order_item.marketplace_sku
    variant_key = _variant_key(order_item.raw_options or {})

    # 1. Store-specific mapping
    mapping = ProductMapping.objects.filter(
        sub_user=sub_user,
        store=store,
        marketplace_sku=sku,
        marketplace_variant=variant_key,
    ).select_related('pc_variant').first()

    # 2. Global mapping (no store)
    if not mapping:
        mapping = ProductMapping.objects.filter(
            sub_user=sub_user,
            store__isnull=True,
            marketplace_sku=sku,
            marketplace_variant=variant_key,
        ).select_related('pc_variant').first()

    # 3. Template-uploaded mapping (no SKU, matches by variant options only)
    if not mapping:
        mapping = ProductMapping.objects.filter(
            sub_user=sub_user,
            marketplace_sku='',
            marketplace_variant=variant_key,
        ).select_related('pc_variant').first()

    if mapping:
        order_item.pc_variant = mapping.pc_variant
        order_item.matched_at = timezone.now()
        order_item.save(update_fields=['pc_variant', 'matched_at'])
        log_event(
            order_item.order, 'matched', actor=actor,
            description=f'{order_item.raw_product_name} → {mapping.pc_variant}',
        )
        if sub_user.auto_design_matching:
            auto_match_design(order_item, actor=actor)
        return True
    return False


def auto_match_order(order, actor=None) -> int:
    """Run auto-match on all unmatched billable items. Returns count of newly matched items."""
    matched = 0
    for item in order.items.filter(pc_variant__isnull=True, unit_price__gte=0):
        if auto_match_item(item, actor=actor):
            matched += 1
    refresh_order_status(order, actor=actor)
    return matched


def order_is_matched(order) -> bool:
    """True if every billable item has a matched catalog variant."""
    return not order.items.filter(pc_variant__isnull=True, unit_price__gte=0).exists()


def order_has_design(order) -> bool:
    """True if at least one order item has a design uploaded."""
    from apps.designs.models import OrderItemDesign
    return OrderItemDesign.objects.filter(order_item__order=order).exists()


def refresh_order_status(order, actor=None) -> bool:
    """Recompute order.status from match + design completeness.

    An order is 'ready' once all billable items are matched AND a design has
    been uploaded; 'matching' once matched but still missing a design.
    Returns True if the order is fully ready (matched + has design).
    """
    from .models import Order
    if not order_is_matched(order):
        return False
    ready = order_has_design(order)
    new_status = Order.Status.READY if ready else Order.Status.MATCHING
    if order.status != new_status:
        order.status = new_status
        order.save(update_fields=['status'])
        if new_status == Order.Status.READY:
            log_event(order, 'ready', actor=actor, description='All items matched and designed')
    return ready


def _normalize_personalization(text):
    """Normalize personalization text for design matching.

    Empty, missing, or marketplace "not requested" placeholders all
    normalize to '' (matches fixed/two-tone designs). Anything else is
    trimmed + lowercased for exact comparison against SkuDesign.personalization_match.
    """
    text = (text or '').strip()
    if text.lower() in ('', 'not requested for this item', 'not requested'):
        return ''
    return text.lower()


def auto_match_design(order_item, actor=None) -> int:
    """
    Try to auto-assign SkuDesigns to an order item's print areas.
    Returns count of areas matched.
    """
    from apps.designs.models import OrderItemDesign, SkuDesign

    if not order_item.pc_variant:
        return 0

    sub_user = order_item.order.sub_user
    sku = order_item.marketplace_sku
    variant = order_item.pc_variant
    personalization = order_item.personalization

    # Determine effective color category
    color_name = variant.color
    is_dark = variant.is_dark

    # 1. Per-product classification (takes precedence over global list)
    from apps.designs.models import SubUserProductColorSetting
    pc_product = variant.pc_product
    product_setting = SubUserProductColorSetting.objects.filter(
        sub_user=sub_user, pc_product=pc_product
    ).prefetch_related('entries').first()

    if product_setting and product_setting.use_custom:
        entry = product_setting.entries.filter(color_name__iexact=color_name).first()
        if entry:
            is_dark = entry.is_dark

    # Find candidate print areas from the product
    if not variant.pc_product:
        return 0

    matched = 0
    for area in variant.pc_product.print_areas.filter(is_active=True):
        if OrderItemDesign.objects.filter(
            order_item=order_item, print_area_name=area.name
        ).exists():
            continue  # already has a design for this area

        design = _find_sku_design(sub_user, sku, area.name, color_name, is_dark, personalization)
        if design:
            log_event(
                order_item.order, 'design_uploaded', actor=actor,
                description=f'{order_item.raw_product_name} — {area.name}: {design.file_name}',
            )
            # Shares the SkuDesign's own physical file (cheap — a design reused across
            # many orders costs no extra storage per order) rather than copying it.
            # Safe because nothing ever physically deletes a design file while another
            # row still points at it — see designs.models.file_in_use(), used by the
            # library/order design delete endpoints.
            OrderItemDesign.objects.create(
                order_item=order_item,
                print_area_name=area.name,
                file=design.file,
                file_name=design.file_name,
                width_px=design.width_px,
                height_px=design.height_px,
                sku_design=design,
            )
            matched += 1

    return matched


def _find_sku_design(sub_user, sku, print_area_name, color_name, is_dark, personalization=''):
    """Priority: personalization match first, then SPECIFIC > DARK/LIGHT > ALL.

    If the item's personalization is non-empty, only designs whose
    personalization_match equals the normalized text are considered — no
    fallback to blank-personalization designs (deliberate: chart-based
    listings require an exact uploaded match).
    """
    from apps.designs.models import SkuDesign

    norm = _normalize_personalization(personalization)
    qs = SkuDesign.objects.filter(
        sub_user=sub_user, sku=sku, print_area_name=print_area_name, personalization_match=norm,
    )

    # 1. Specific color
    design = qs.filter(color_scope=SkuDesign.ColorScope.SPECIFIC, specific_color=color_name).first()
    if design:
        return design

    # 2. Dark / Light
    scope = SkuDesign.ColorScope.DARK if is_dark else SkuDesign.ColorScope.LIGHT
    design = qs.filter(color_scope=scope).first()
    if design:
        return design

    # 3. All colors
    return qs.filter(color_scope=SkuDesign.ColorScope.ALL).first()


def parse_mapping_excel(file_obj, sub_user, store=None):
    """
    Parse a product mapping Excel file.

    Row 1 = header (fixed column positions):
      A: Catalog Product (product name — case-insensitive lookup)
      B: Catalog Size
      C: Catalog Color
      D: Option 1 Name   (marketplace option name, e.g. "Item & Size")
      E: Option 1 Value  (marketplace option value, e.g. "Unisex T-Shirt - M")
      F: Option 2 Name   (optional, e.g. "Clothing Color")
      G: Option 2 Value  (optional, e.g. "Sea Foam")
      H: Option 3 Name   (optional)
      I: Option 3 Value  (optional)
      J: Option 4 Name   (optional)
      K: Option 4 Value  (optional)

    Returns (saved_count, skipped_count, errors).
    """
    import openpyxl
    from apps.catalog.models import PrintCenterProduct
    from .models import ProductMapping

    wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
    ws = wb.active

    created_count, updated_count, skipped, errors = 0, 0, 0, []

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not any(row):
            continue

        def cell(i):
            return str(row[i] or '').strip() if i < len(row) else ''

        product_name = cell(0)
        size         = cell(1)
        color        = cell(2)

        if not product_name or not cell(3) or not cell(4):
            skipped += 1
            continue

        # Collect all non-empty option pairs (columns D-K, up to 4 pairs)
        options = {}
        for pair_idx in range(4):
            name_col  = 3 + pair_idx * 2
            value_col = 4 + pair_idx * 2
            name  = cell(name_col)
            value = cell(value_col)
            if name and value:
                options[name] = value

        marketplace_variant = _variant_key(options)

        # Look up product by name (exact, then partial)
        pc_product = PrintCenterProduct.objects.filter(
            print_center=sub_user.print_center, is_active=True, name__iexact=product_name
        ).first()
        if not pc_product:
            pc_product = PrintCenterProduct.objects.filter(
                print_center=sub_user.print_center, is_active=True, name__icontains=product_name
            ).first()
        if not pc_product:
            errors.append(f'Row {row_idx}: catalog product "{product_name}" not found')
            skipped += 1
            continue

        variant_qs = pc_product.variants.filter(is_active=True)
        if size:
            variant_qs = variant_qs.filter(size__iexact=size)
        if color:
            variant_qs = variant_qs.filter(color__iexact=color)
        pc_variant = variant_qs.first()

        if not pc_variant:
            errors.append(f'Row {row_idx}: no variant found in "{pc_product.name}" size={size} color={color}')
            skipped += 1
            continue

        _, created = ProductMapping.objects.update_or_create(
            sub_user=sub_user,
            store=store,
            marketplace_sku='',
            marketplace_variant=marketplace_variant,
            defaults={'pc_variant': pc_variant},
        )
        if created:
            created_count += 1
        else:
            updated_count += 1

    return created_count, updated_count, skipped, errors


def generate_mapping_template(sub_user):
    """
    Build an Excel template pre-filled from unmatched order items.

    D-G columns are pre-filled with the exact raw_options values from actual
    unmatched orders so the user cannot accidentally mistype them. A-C columns
    are left empty (or pre-filled from an existing mapping if one exists).
    De-duplicated by marketplace_variant key.
    Returns an openpyxl Workbook.
    """
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Mappings'

    cat_fill   = PatternFill(fill_type='solid', fgColor='DCFCE7')
    mkt_fill   = PatternFill(fill_type='solid', fgColor='DBEAFE')
    bold       = Font(bold=True)
    hint_font  = Font(italic=True, color='999999')

    headers = ['Catalog Product', 'Catalog Size', 'Catalog Color',
               'Option 1 Name', 'Option 1 Value',
               'Option 2 Name', 'Option 2 Value',
               'Option 3 Name', 'Option 3 Value',
               'Option 4 Name', 'Option 4 Value']
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.font = bold
        c.fill = cat_fill if col <= 3 else mkt_fill
        c.alignment = Alignment(horizontal='center')

    from .models import OrderItem, ProductMapping

    # Collect unmatched items — unit_price >= 0, no catalog variant assigned
    unmatched_items = (
        OrderItem.objects
        .filter(
            order__sub_user=sub_user,
            order__is_hidden=False,
            pc_variant__isnull=True,
            unit_price__gte=0,
        )
        .select_related('order__store')
    )

    # Build lookup: variant_key → existing ProductMapping (if any)
    existing_mappings = {
        m.marketplace_variant: m
        for m in ProductMapping.objects.filter(sub_user=sub_user).select_related('pc_variant__pc_product')
    }

    seen_keys = set()
    row_idx = 2

    for item in unmatched_items:
        raw_opts = item.raw_options or {}
        variant_key = _variant_key(raw_opts)

        if variant_key in seen_keys:
            continue
        seen_keys.add(variant_key)

        # Parse ALL option pairs from raw_opts (up to 4, preserving original order)
        pairs = list(raw_opts.items())[:4]

        # Pre-fill catalog columns if a mapping already exists for this key
        existing = existing_mappings.get(variant_key)
        catalog_name  = existing.pc_variant.pc_product.name if existing and existing.pc_variant else ''
        catalog_size  = existing.pc_variant.size            if existing and existing.pc_variant else ''
        catalog_color = existing.pc_variant.color           if existing and existing.pc_variant else ''

        row_data = [catalog_name, catalog_size, catalog_color]
        for name, value in pairs:
            row_data += [name, value]
        # Pad to 11 columns (3 catalog + 4×2 option)
        row_data += [''] * (11 - len(row_data))

        for col, val in enumerate(row_data, 1):
            c = ws.cell(row=row_idx, column=col, value=val)
            if col <= 3 and not val:
                c.font = hint_font
        row_idx += 1

    # If no unmatched items, add a hint row
    if row_idx == 2:
        for col, val in enumerate(['(no unmatched orders)'] + [''] * 10, 1):
            ws.cell(row=2, column=col, value=val).font = hint_font

    for col, w in enumerate([36, 12, 24, 18, 28, 18, 28, 18, 28, 18, 28], 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = w

    return wb


def export_saved_mappings(sub_user):
    """
    Build an Excel file with all saved ProductMappings for a sub-user.
    Both catalog and marketplace columns are pre-filled.
    Returns an openpyxl Workbook.
    """
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from .models import ProductMapping

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Saved Mappings'

    cat_fill    = PatternFill(fill_type='solid', fgColor='DCFCE7')
    mkt_fill    = PatternFill(fill_type='solid', fgColor='DBEAFE')
    bold        = Font(bold=True)
    store_fill  = PatternFill(fill_type='solid', fgColor='FEF9C3')

    headers = ['Catalog Product', 'Catalog Size', 'Catalog Color',
               'Option 1 Name', 'Option 1 Value', 'Option 2 Name', 'Option 2 Value',
               'Store']
    fills   = [cat_fill, cat_fill, cat_fill, mkt_fill, mkt_fill, mkt_fill, mkt_fill, store_fill]
    for col, (h, f) in enumerate(zip(headers, fills), 1):
        c = ws.cell(row=1, column=col, value=h)
        c.font = bold
        c.fill = f
        c.alignment = Alignment(horizontal='center')

    mappings = (
        ProductMapping.objects
        .filter(sub_user=sub_user)
        .select_related('pc_variant__pc_product', 'store')
        .order_by('pc_variant__pc_product__name', 'marketplace_variant')
    )

    for row_idx, m in enumerate(mappings, start=2):
        variant = m.pc_variant

        # Parse marketplace_variant key back to option pairs ("k:v|k:v")
        pairs = []
        for part in (m.marketplace_variant or '').split('|'):
            if ':' in part:
                name, value = part.split(':', 1)
                pairs.append((name.strip(), value.strip()))

        opt1_name  = pairs[0][0] if len(pairs) > 0 else ''
        opt1_value = pairs[0][1] if len(pairs) > 0 else ''
        opt2_name  = pairs[1][0] if len(pairs) > 1 else ''
        opt2_value = pairs[1][1] if len(pairs) > 1 else ''

        ws.cell(row=row_idx, column=1, value=variant.pc_product.name if variant else '')
        ws.cell(row=row_idx, column=2, value=variant.size if variant else '')
        ws.cell(row=row_idx, column=3, value=variant.color if variant else '')
        ws.cell(row=row_idx, column=4, value=opt1_name)
        ws.cell(row=row_idx, column=5, value=opt1_value)
        ws.cell(row=row_idx, column=6, value=opt2_name)
        ws.cell(row=row_idx, column=7, value=opt2_value)
        ws.cell(row=row_idx, column=8, value=m.store.name if m.store else '')

    for col, w in enumerate([36, 12, 24, 18, 28, 18, 28, 20], 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = w

    return wb


