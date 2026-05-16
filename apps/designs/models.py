from django.db import models

from apps.core.models import TimeStampedModel


class ColorCategoryList(TimeStampedModel):
    """
    Sub-user's custom dark/light color categorization.
    Overrides the default is_dark flag on PrintCenterVariant.
    """
    class Category(models.TextChoices):
        DARK = 'dark', 'Dark'
        LIGHT = 'light', 'Light'

    sub_user = models.ForeignKey(
        'accounts.SubUserProfile', related_name='color_categories', on_delete=models.CASCADE
    )
    category = models.CharField(max_length=5, choices=Category.choices)
    colors = models.JSONField(default=list)  # ["Military Green", "Black", ...]

    class Meta:
        unique_together = ('sub_user', 'category')

    def __str__(self):
        return f'{self.sub_user} — {self.category} ({len(self.colors)} colors)'


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
    file = models.FileField(upload_to='designs/sku/')
    file_name = models.CharField(max_length=255, blank=True)
    width_px = models.PositiveIntegerField(default=0)
    height_px = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('sub_user', 'sku', 'print_area_name', 'color_scope', 'specific_color')
        indexes = [models.Index(fields=['sub_user', 'sku', 'print_area_name'])]

    def __str__(self):
        return f'{self.sku} / {self.print_area_name} [{self.color_scope}]'


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
