"""Order processing service functions."""
from django.utils import timezone


def _variant_key(raw_options: dict) -> str:
    """Canonical string key from raw_options dict for mapping lookup."""
    return "|".join(f"{k}:{v}" for k, v in sorted(raw_options.items()))


def auto_match_item(order_item) -> bool:
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

    if mapping:
        order_item.pc_variant = mapping.pc_variant
        order_item.matched_at = timezone.now()
        order_item.save(update_fields=['pc_variant', 'matched_at'])
        return True
    return False


def auto_match_order(order) -> int:
    """Run auto-match on all unmatched items. Returns count of newly matched items."""
    matched = 0
    for item in order.items.filter(pc_variant__isnull=True):
        if auto_match_item(item):
            matched += 1
    _update_order_status_after_match(order)
    return matched


def _update_order_status_after_match(order):
    from .models import Order
    if not order.items.filter(pc_variant__isnull=True).exists():
        order.status = Order.Status.READY
        order.save(update_fields=['status'])


def auto_match_design(order_item) -> int:
    """
    Try to auto-assign SkuDesigns to an order item's print areas.
    Returns count of areas matched.
    """
    from apps.designs.models import ColorCategoryList, OrderItemDesign, SkuDesign

    if not order_item.pc_variant:
        return 0

    sub_user = order_item.order.sub_user
    sku = order_item.marketplace_sku
    variant = order_item.pc_variant

    # Determine effective color category
    color_name = variant.color
    is_dark = variant.is_dark

    # Override with sub-user's custom color list if defined
    dark_list = ColorCategoryList.objects.filter(
        sub_user=sub_user, category=ColorCategoryList.Category.DARK
    ).first()
    light_list = ColorCategoryList.objects.filter(
        sub_user=sub_user, category=ColorCategoryList.Category.LIGHT
    ).first()

    if dark_list and color_name in dark_list.colors:
        is_dark = True
    elif light_list and color_name in light_list.colors:
        is_dark = False

    # Find candidate print areas from the product
    if not variant.pc_product:
        return 0

    matched = 0
    for area in variant.pc_product.print_areas.filter(is_active=True):
        if OrderItemDesign.objects.filter(
            order_item=order_item, print_area_name=area.name
        ).exists():
            continue  # already has a design for this area

        design = _find_sku_design(sub_user, sku, area.name, color_name, is_dark)
        if design:
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


def _find_sku_design(sub_user, sku, print_area_name, color_name, is_dark):
    """Priority: SPECIFIC > DARK/LIGHT > ALL."""
    from apps.designs.models import SkuDesign

    qs = SkuDesign.objects.filter(sub_user=sub_user, sku=sku, print_area_name=print_area_name)

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

    Expected columns (row 1 = header):
      A: marketplace_sku   (optional)
      B: marketplace_variant  e.g. "Size: XL | Color: Black"
      C: pc_product_sku
      D: size
      E: color

    Returns (saved_count, skipped_count, errors).
    """
    import openpyxl
    from apps.catalog.models import PrintCenterProduct, PrintCenterVariant
    from .models import ProductMapping

    wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
    ws = wb.active

    saved, skipped, errors = 0, 0, []

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not any(row):
            continue

        marketplace_sku = str(row[0] or '').strip()
        marketplace_variant = str(row[1] or '').strip()
        pc_product_sku = str(row[2] or '').strip()
        size = str(row[3] or '').strip()
        color = str(row[4] or '').strip()

        if not marketplace_variant or not pc_product_sku:
            skipped += 1
            continue

        # Find matching PrintCenterVariant
        try:
            pc_product = PrintCenterProduct.objects.get(
                print_center=sub_user.print_center, sku=pc_product_sku
            )
        except PrintCenterProduct.DoesNotExist:
            errors.append(f'Row {row_idx}: product SKU "{pc_product_sku}" not found')
            skipped += 1
            continue

        variant_qs = pc_product.variants.filter(is_active=True)
        if size:
            variant_qs = variant_qs.filter(size__iexact=size)
        if color:
            variant_qs = variant_qs.filter(color__iexact=color)
        pc_variant = variant_qs.first()

        if not pc_variant:
            errors.append(f'Row {row_idx}: no variant found for {pc_product_sku} size={size} color={color}')
            skipped += 1
            continue

        ProductMapping.objects.update_or_create(
            sub_user=sub_user,
            store=store,
            marketplace_sku=marketplace_sku,
            marketplace_variant=marketplace_variant,
            defaults={'pc_variant': pc_variant},
        )
        saved += 1

    return saved, skipped, errors


def parse_color_category_excel(file_obj, sub_user):
    """
    Parse a color category Excel file.

    Expected columns:
      A: dark_color
      B: light_color

    Row 1 = header. Returns (dark_count, light_count).
    """
    import openpyxl
    from apps.designs.models import ColorCategoryList

    wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
    ws = wb.active

    dark_colors, light_colors = [], []
    for row in ws.iter_rows(min_row=2, values_only=True):
        dark = str(row[0] or '').strip()
        light = str(row[1] or '').strip() if len(row) > 1 else ''
        if dark:
            dark_colors.append(dark)
        if light:
            light_colors.append(light)

    if dark_colors:
        ColorCategoryList.objects.update_or_create(
            sub_user=sub_user,
            category=ColorCategoryList.Category.DARK,
            defaults={'colors': dark_colors},
        )
    if light_colors:
        ColorCategoryList.objects.update_or_create(
            sub_user=sub_user,
            category=ColorCategoryList.Category.LIGHT,
            defaults={'colors': light_colors},
        )
    return len(dark_colors), len(light_colors)
