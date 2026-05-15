from django.contrib import admin

from .models import (
    Brand,
    Category,
    PrintArea,
    PrintCenterPrintArea,
    PrintCenterProduct,
    PrintCenterVariant,
    Product,
    ProductImage,
    ProductVariant,
)


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 0


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0
    fields = ('sku', 'color', 'size', 'cost', 'price', 'is_dark', 'weight_oz', 'stock_status')


class PrintAreaInline(admin.TabularInline):
    model = PrintArea
    extra = 0
    fields = ('name', 'is_default', 'width_inches', 'height_inches', 'x', 'y', 'width_px', 'height_px')


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'sku', 'brand', 'category', 'base_price', 'badge', 'is_active')
    list_filter = ('brand', 'category', 'badge', 'is_active')
    search_fields = ('name', 'sku')
    inlines = [ProductImageInline, ProductVariantInline, PrintAreaInline]


admin.site.register(Brand)
admin.site.register(Category)


class PrintCenterVariantInline(admin.TabularInline):
    model = PrintCenterVariant
    extra = 0
    fields = ('color', 'size', 'cost', 'weight_oz', 'is_dark', 'is_active')


class PrintCenterPrintAreaInline(admin.TabularInline):
    model = PrintCenterPrintArea
    extra = 0
    fields = ('name', 'is_default', 'is_active', 'width_inches', 'height_inches')


@admin.register(PrintCenterProduct)
class PrintCenterProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'sku', 'print_center', 'is_active', 'created_at')
    list_filter = ('print_center', 'is_active')
    search_fields = ('name', 'sku', 'print_center__company_name')
    inlines = [PrintCenterVariantInline, PrintCenterPrintAreaInline]
    readonly_fields = ('source_product', 'created_at', 'updated_at')
