from django.db import models

from apps.core.models import TimeStampedModel


class Category(TimeStampedModel):
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    parent = models.ForeignKey('self', null=True, blank=True, related_name='children', on_delete=models.SET_NULL)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('sort_order', 'name')
        verbose_name_plural = 'categories'

    def __str__(self):
        return self.name


class Brand(TimeStampedModel):
    name = models.CharField(max_length=120, unique=True)

    def __str__(self):
        return self.name


VARIATION_TYPES = (
    ('color', 'Color'),
    ('size', 'Size'),
    ('dimension', 'Dimension'),
    ('volume', 'Volume'),
    ('print_area', 'Print Area'),
)

BADGE_CHOICES = (
    ('popular', 'Popular'),
    ('new', 'New'),
    ('bestseller', 'Bestseller'),
)


class Product(TimeStampedModel):
    category = models.ForeignKey(Category, related_name='products', on_delete=models.PROTECT)
    brand = models.ForeignKey(Brand, related_name='products', on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=64, unique=True)
    description = models.TextField(blank=True)
    base_cost = models.DecimalField(max_digits=10, decimal_places=2)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    badge = models.CharField(max_length=16, choices=BADGE_CHOICES, blank=True)
    is_active = models.BooleanField(default=True)
    primary_image = models.ImageField(upload_to='products/', blank=True, null=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.name} ({self.sku})'


class ProductImage(TimeStampedModel):
    product = models.ForeignKey(Product, related_name='images', on_delete=models.CASCADE)
    image = models.ImageField(upload_to='products/')
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('sort_order',)


class ProductVariationType(models.Model):
    product = models.ForeignKey(Product, related_name='variation_types', on_delete=models.CASCADE)
    type = models.CharField(max_length=16, choices=VARIATION_TYPES)

    class Meta:
        unique_together = ('product', 'type')


class ProductVariant(TimeStampedModel):
    product = models.ForeignKey(Product, related_name='variants', on_delete=models.CASCADE)
    sku = models.CharField(max_length=80, unique=True)
    color = models.CharField(max_length=40, blank=True)
    color_hex = models.CharField(max_length=7, blank=True)
    size = models.CharField(max_length=20, blank=True)
    dimension = models.CharField(max_length=40, blank=True)
    volume = models.CharField(max_length=40, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    is_dark = models.BooleanField(default=False, help_text='Dark garment — uses dark design variant')
    stock_status = models.CharField(max_length=20, default='in_stock')

    class Meta:
        ordering = ('size', 'color')

    def __str__(self):
        return self.sku


class PrintArea(models.Model):
    product = models.ForeignKey(Product, related_name='print_areas', on_delete=models.CASCADE)
    name = models.CharField(max_length=40, help_text='Front, Back, Left Sleeve, Right Sleeve, Left Chest, Collar')
    extra_cost = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    is_default = models.BooleanField(default=False)
    # Coordinates for print preview placement on mockup
    x = models.PositiveIntegerField(default=0)
    y = models.PositiveIntegerField(default=0)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('product', 'name')
