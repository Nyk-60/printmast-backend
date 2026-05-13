from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class Design(TimeStampedModel):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='designs', on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    dark_file = models.ImageField(upload_to='designs/dark/', blank=True, null=True)
    light_file = models.ImageField(upload_to='designs/light/', blank=True, null=True)

    def __str__(self):
        return f'{self.owner.email} — {self.name}'


class UserSkuDesign(TimeStampedModel):
    """Maps a user's catalog SKU + print area to a saved design (dark/light variants)."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='sku_designs', on_delete=models.CASCADE)
    sku = models.CharField(max_length=80, db_index=True)
    print_area = models.CharField(max_length=40, help_text='Front, Back, Left Sleeve, etc.')
    dark_design = models.ImageField(upload_to='designs/sku/dark/', blank=True, null=True)
    light_design = models.ImageField(upload_to='designs/sku/light/', blank=True, null=True)

    class Meta:
        unique_together = ('owner', 'sku', 'print_area')


class OrderDesign(TimeStampedModel):
    """Concrete design assignment per OrderItem & print area."""
    order_item = models.ForeignKey('orders.OrderItem', related_name='order_designs', on_delete=models.CASCADE)
    print_area = models.CharField(max_length=40)
    design_file = models.ImageField(upload_to='order_designs/')
    preview_image = models.ImageField(upload_to='order_designs/previews/', blank=True, null=True)

    class Meta:
        unique_together = ('order_item', 'print_area')
