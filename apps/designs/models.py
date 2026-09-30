from django.db import models

from apps.core.models import TimeStampedModel


class SubUserProductColorSetting(TimeStampedModel):
    """
    Per-product color classification preference for a sub-user.
    When use_custom=True, entries override the print center's is_dark flag.
    When use_custom=False, the print center's is_dark flag is used directly
    (bypassing the global ColorCategoryList for this product).
    If no record exists, falls back to the global ColorCategoryList.
    """
    sub_user   = models.ForeignKey(
        'accounts.SubUserProfile', related_name='product_color_settings', on_delete=models.CASCADE
    )
    pc_product = models.ForeignKey(
        'catalog.PrintCenterProduct', related_name='sub_user_color_settings', on_delete=models.CASCADE
    )
    use_custom = models.BooleanField(default=False)

    class Meta:
        unique_together = ('sub_user', 'pc_product')

    def __str__(self):
        return f'{self.sub_user} / {self.pc_product} — {"custom" if self.use_custom else "pc"}'


class SubUserProductColorEntry(TimeStampedModel):
    """One color → dark/light mapping for a SubUserProductColorSetting."""
    setting    = models.ForeignKey(
        SubUserProductColorSetting, related_name='entries', on_delete=models.CASCADE
    )
    color_name = models.CharField(max_length=100)
    is_dark    = models.BooleanField()

    class Meta:
        unique_together = ('setting', 'color_name')

    def __str__(self):
        return f'{self.color_name} → {"dark" if self.is_dark else "light"}'


class SkuDesign(TimeStampedModel):
    """
    Reusable design saved for a SKU + print area.
    Auto-applied to future orders matching the same SKU and color scope.
    """
    class ColorScope(models.TextChoices):
        ALL = 'all', 'All Colors'
        DARK = 'dark', 'Dark Shirts Only'
        LIGHT = 'light', 'Light Shirts Only'
        SPECIFIC = 'specific', 'Specific Color'

    sub_user = models.ForeignKey(
        'accounts.SubUserProfile', related_name='sku_designs', on_delete=models.CASCADE
    )
    sku = models.CharField(max_length=200, db_index=True)
    print_area_name = models.CharField(max_length=40)  # Front, Back, Left Sleeve…
    color_scope = models.CharField(max_length=8, choices=ColorScope.choices, default=ColorScope.ALL)
    specific_color = models.CharField(max_length=80, blank=True)  # only for SPECIFIC scope
    personalization_match = models.CharField(
        max_length=300, blank=True, default='',
        help_text='Normalized personalization text required for this design to auto-match. '
                  'Blank = matches items with no/empty personalization.',
    )
    file = models.FileField(upload_to='designs/sku/')
    file_name = models.CharField(max_length=255, blank=True)
    width_px = models.PositiveIntegerField(default=0)
    height_px = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = (
            'sub_user', 'sku', 'print_area_name', 'color_scope', 'specific_color', 'personalization_match',
        )
        indexes = [models.Index(fields=['sub_user', 'sku', 'print_area_name'])]

    def __str__(self):
        return f'{self.sku} / {self.print_area_name} [{self.color_scope}]'


def file_in_use(file_name, *, exclude_sku_design_id=None, exclude_order_item_design_id=None):
    """Is this exact stored file path still referenced by any OTHER design row?

    auto_match_design() gives an OrderItemDesign the SAME physical file as the
    SkuDesign it matched, rather than a copy — reusing one design across many
    orders costs no extra storage per order. That sharing is only safe if a
    delete NEVER removes the bytes while another row still points at them: a
    print center relies on an already-matched order's design surviving even
    after the sub-user edits or deletes the reusable library entry, to settle
    "I never uploaded that" disputes on orders completed long ago. Callers
    must check this before physically deleting a design file, excluding only
    the very row being deleted.
    """
    if not file_name:
        return False
    oid_qs = OrderItemDesign.objects.filter(file=file_name)
    if exclude_order_item_design_id:
        oid_qs = oid_qs.exclude(pk=exclude_order_item_design_id)
    if oid_qs.exists():
        return True
    sd_qs = SkuDesign.objects.filter(file=file_name)
    if exclude_sku_design_id:
        sd_qs = sd_qs.exclude(pk=exclude_sku_design_id)
    return sd_qs.exists()


class OrderItemDesign(TimeStampedModel):
    """
    Design assigned to a specific order item + print area.
    May be auto-matched from a SkuDesign or uploaded one-time for this order.
    """
    order_item = models.ForeignKey(
        'orders.OrderItem', related_name='designs', on_delete=models.CASCADE
    )
    print_area_name = models.CharField(max_length=40)
    file = models.FileField(upload_to='designs/orders/')
    file_name = models.CharField(max_length=255, blank=True)
    width_px = models.PositiveIntegerField(default=0)
    height_px = models.PositiveIntegerField(default=0)
    sku_design = models.ForeignKey(
        SkuDesign, null=True, blank=True,
        related_name='order_uses', on_delete=models.SET_NULL
    )

    class Meta:
        unique_together = ('order_item', 'print_area_name')

    def __str__(self):
        return f'Order {self.order_item.order_id} / {self.print_area_name}'
