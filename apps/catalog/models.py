from django.db import models
from django.utils.text import slugify

from apps.core.models import TimeStampedModel


class Category(TimeStampedModel):
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True, blank=True)
    parent = models.ForeignKey('self', null=True, blank=True, related_name='children', on_delete=models.SET_NULL)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('sort_order', 'name')
        verbose_name_plural = 'categories'

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)
            slug, n = base, 1
            while Category.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f'{base}-{n}'
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)


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
    sku = models.CharField(max_length=64, blank=True, default='')
    description = models.TextField(blank=True)
    base_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    badge = models.CharField(max_length=16, choices=BADGE_CHOICES, blank=True)
    is_active = models.BooleanField(default=True)
    primary_image = models.ImageField(upload_to='products/', blank=True, null=True)
    variant1_label = models.CharField(max_length=40, default='Size')
    variant2_label = models.CharField(max_length=40, default='Color')

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
    sku = models.CharField(max_length=80, blank=True, default='')
    color = models.CharField(max_length=40, blank=True)
    color_hex = models.CharField(max_length=7, blank=True)
    color_group = models.CharField(max_length=40, blank=True, help_text='e.g. Light, Dark, Neutral')
    size = models.CharField(max_length=20, blank=True)
    dimension = models.CharField(max_length=40, blank=True)
    volume = models.CharField(max_length=40, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_dark = models.BooleanField(default=False, help_text='Dark garment — uses dark design variant')
    stock_status = models.CharField(max_length=20, default='in_stock')
    weight_oz = models.DecimalField(max_digits=8, decimal_places=2, default=0, help_text='Weight in ounces for shipping')

    SIZE_ORDER = ['XS','S','SM','M','ML','L','XL','1XL','2XL','XXL','3XL','XXXL','4XL','5XL','6XL']

    class Meta:
        ordering = ('size', 'color')

    def __str__(self):
        return self.sku


class PrintArea(models.Model):
    product = models.ForeignKey(Product, related_name='print_areas', on_delete=models.CASCADE)
    name = models.CharField(max_length=40, help_text='Front, Back, Sleeve, Pocket, Neck')
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
    sku = models.CharField(max_length=64, blank=True, default='')
    description = models.TextField(blank=True)
    category = models.ForeignKey(Category, related_name='pc_products', on_delete=models.PROTECT, null=True, blank=True)
    brand = models.ForeignKey(Brand, related_name='pc_products', on_delete=models.PROTECT, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    primary_image = models.ImageField(upload_to='pc_products/', blank=True, null=True)
    badge = models.CharField(max_length=16, choices=BADGE_CHOICES, blank=True)
    variant1_label = models.CharField(max_length=40, default='Size')
    variant2_label = models.CharField(max_length=40, default='Color')

    class Meta:
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
    color_group = models.CharField(max_length=40, blank=True)
    size = models.CharField(max_length=20, blank=True)
    is_dark = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2)
    weight_oz = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    # Package dimensions (inches) — used for shipping rate calculation
    dim_length_in         = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    dim_width_in          = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    dim_height_base_in    = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    dim_height_per_unit_in = models.DecimalField(max_digits=5, decimal_places=3, default=0,
        help_text='Height increment added every N items (see dim_height_step)')
    dim_height_step = models.PositiveIntegerField(default=1,
        help_text='H increases every N items: 1=every item, 5=every 5 items (e.g. thin DTF prints)')
    is_stackable = models.BooleanField(default=True,
        help_text='If True, height compounds with quantity. If False (e.g. canvas), max height is used.')

    class Meta:
        ordering = ('size', 'color')

    def __str__(self):
        parts = [self.pc_product.sku]
        if self.size:
            parts.append(self.size)
        if self.color:
            parts.append(self.color)
        return ' / '.join(parts)


class VariantDimensionRule(models.Model):
    """Explicit quantity-based dimension override for a variant.

    Takes precedence over the formula (dim_height_base_in + dim_height_per_unit_in).
    Example: qty 1–1 → 12×10×1,  qty 2–4 → 12×10×2,  qty 5+ → 12×10×3
    """
    variant = models.ForeignKey(PrintCenterVariant, related_name='dimension_rules', on_delete=models.CASCADE)
    qty_min  = models.PositiveIntegerField()
    qty_max  = models.PositiveIntegerField(null=True, blank=True, help_text='Leave blank for no upper limit')
    length_in = models.DecimalField(max_digits=6, decimal_places=2)
    width_in  = models.DecimalField(max_digits=6, decimal_places=2)
    height_in = models.DecimalField(max_digits=6, decimal_places=2)

    class Meta:
        ordering = ('qty_min',)

    def __str__(self):
        max_str = str(self.qty_max) if self.qty_max else '∞'
        return f'{self.variant} qty {self.qty_min}–{max_str}: {self.length_in}×{self.width_in}×{self.height_in}"'


def default_size_mode(name):
    """Sleeves are sized by a width AND a height; every other area by one max dimension."""
    return 'box' if 'sleeve' in (name or '').lower() else 'longest'


class PrintCenterPrintArea(models.Model):
    """Print areas available for a print center's product."""

    class SizeMode(models.TextChoices):
        LONGEST = 'longest', 'One max dimension (longest side)'
        BOX = 'box', 'Width × height'

    pc_product = models.ForeignKey(PrintCenterProduct, related_name='print_areas', on_delete=models.CASCADE)
    source_print_area = models.ForeignKey(
        PrintArea, related_name='pc_copies', on_delete=models.SET_NULL, null=True, blank=True
    )
    name = models.CharField(max_length=40)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    width_inches = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    height_inches = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    icon = models.ImageField(upload_to='print_areas/', blank=True, null=True)
    size_mode = models.CharField(max_length=10, choices=SizeMode.choices, default=SizeMode.LONGEST)

    class Meta:
        unique_together = ('pc_product', 'name')

    def __str__(self):
        return f'{self.pc_product.name} — {self.name}'


class PrintAreaSizeSpec(models.Model):
    """Print size (inches) for a specific print area + garment size combination.

    'longest' areas use max_dim_in (the design's longer side, aspect kept). 'box' areas
    (sleeves) use max_width_in x max_height_in: the design is scaled, aspect kept, until
    it hits the width or the height, whichever comes first.
    """
    print_area = models.ForeignKey(PrintCenterPrintArea, related_name='size_specs', on_delete=models.CASCADE)
    size       = models.CharField(max_length=20)
    max_dim_in = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    max_width_in  = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    max_height_in = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        unique_together = ('print_area', 'size')

    def __str__(self):
        if self.print_area.size_mode == PrintCenterPrintArea.SizeMode.BOX:
            return f'{self.print_area} / {self.size}: {self.max_width_in}" x {self.max_height_in}"'
        return f'{self.print_area} / {self.size}: {self.max_dim_in}"'
