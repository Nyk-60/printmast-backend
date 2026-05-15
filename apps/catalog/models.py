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


BADGE_CHOICES = (
    ('popular', 'Popular'),
    ('new', 'New'),
    ('bestseller', 'Bestseller'),
)


# ── Platform Products (created by SuperAdmin) ─────────────────────────────────

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
    weight_oz = models.DecimalField(max_digits=8, decimal_places=2, default=0, help_text='Weight in ounces for shipping')

    class Meta:
        ordering = ('size', 'color')

    def __str__(self):
        return self.sku


class PrintArea(models.Model):
    product = models.ForeignKey(Product, related_name='print_areas', on_delete=models.CASCADE)
    name = models.CharField(max_length=40, help_text='Front, Back, Left Sleeve, Right Sleeve, Left Chest, Collar')
    is_default = models.BooleanField(default=False)
    # Physical print dimensions in inches (used for gang sheet layout)
    width_inches = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    height_inches = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    # Mockup preview positioning (pixels)
    x = models.PositiveIntegerField(default=0)
    y = models.PositiveIntegerField(default=0)
    width_px = models.PositiveIntegerField(default=0)
    height_px = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('product', 'name')

    def __str__(self):
        return f'{self.product.name} — {self.name}'


# ── Print Center Catalog (adopted from platform or created from scratch) ───────

class PrintCenterProduct(TimeStampedModel):
    """A print center's catalog entry. Optionally copied from a platform Product."""
    print_center = models.ForeignKey(
        'accounts.PrintCenterProfile', related_name='catalog_products', on_delete=models.CASCADE
    )
    source_product = models.ForeignKey(
        Product, related_name='print_center_copies', on_delete=models.SET_NULL, null=True, blank=True
    )
    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=64)
    description = models.TextField(blank=True)
    category = models.ForeignKey(Category, related_name='pc_products', on_delete=models.PROTECT, null=True, blank=True)
    brand = models.ForeignKey(Brand, related_name='pc_products', on_delete=models.PROTECT, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    primary_image = models.ImageField(upload_to='pc_products/', blank=True, null=True)
    badge = models.CharField(max_length=16, choices=BADGE_CHOICES, blank=True)

    class Meta:
        unique_together = ('print_center', 'sku')
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.name} [{self.print_center.company_name}]'


class PrintCenterVariant(TimeStampedModel):
    """A variant in the print center's catalog with their own pricing."""
    pc_product = models.ForeignKey(PrintCenterProduct, related_name='variants', on_delete=models.CASCADE)
    source_variant = models.ForeignKey(
        ProductVariant, related_name='pc_copies', on_delete=models.SET_NULL, null=True, blank=True
    )
    color = models.CharField(max_length=40, blank=True)
    color_hex = models.CharField(max_length=7, blank=True)
    size = models.CharField(max_length=20, blank=True)
    is_dark = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2)
    weight_oz = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    class Meta:
        ordering = ('size', 'color')

    def __str__(self):
        parts = [self.pc_product.sku]
        if self.size:
            parts.append(self.size)
        if self.color:
            parts.append(self.color)
        return ' / '.join(parts)


class PrintCenterPrintArea(models.Model):
    """Print areas available for a print center's product."""
    pc_product = models.ForeignKey(PrintCenterProduct, related_name='print_areas', on_delete=models.CASCADE)
    source_print_area = models.ForeignKey(
        PrintArea, related_name='pc_copies', on_delete=models.SET_NULL, null=True, blank=True
    )
    name = models.CharField(max_length=40)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    width_inches = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    height_inches = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    class Meta:
        unique_together = ('pc_product', 'name')

    def __str__(self):
        return f'{self.pc_product.name} — {self.name}'
