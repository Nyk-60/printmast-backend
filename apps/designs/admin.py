from django.contrib import admin

from .models import OrderItemDesign, SkuDesign


@admin.register(SkuDesign)
class SkuDesignAdmin(admin.ModelAdmin):
    list_display = ('sku', 'print_area_name', 'color_scope', 'specific_color', 'sub_user', 'created_at')
    list_filter = ('color_scope', 'print_area_name')
    search_fields = ('sku', 'sub_user__user__email')


@admin.register(OrderItemDesign)
class OrderItemDesignAdmin(admin.ModelAdmin):
    list_display = ('order_item', 'print_area_name', 'file_name', 'sku_design')
    search_fields = ('order_item__order__shipstation_order_number',)
