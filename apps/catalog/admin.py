from django.contrib import admin

from .models import Brand, Category, PrintArea, Product, ProductImage, ProductVariant, ProductVariationType


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 0


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0


class PrintAreaInline(admin.TabularInline):
    model = PrintArea
    extra = 0


class ProductVariationTypeInline(admin.TabularInline):
    model = ProductVariationType
    extra = 0


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'sku', 'brand', 'category', 'base_price', 'badge', 'is_active')
    list_filter = ('brand', 'category', 'badge', 'is_active')
    search_fields = ('name', 'sku')
    inlines = [ProductImageInline, ProductVariationTypeInline, ProductVariantInline, PrintAreaInline]


admin.site.register(Brand)
admin.site.register(Category)
