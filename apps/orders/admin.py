from django.contrib import admin

from .models import Order, OrderItem, ProductMapping


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    fields = ('marketplace_sku', 'raw_product_name', 'quantity', 'pc_variant', 'matched_at')
    readonly_fields = ('matched_at',)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('shipstation_order_number', 'customer_name', 'store', 'status', 'ordered_at')
    list_filter = ('status', 'store__shipstation_account__print_center')
    search_fields = ('shipstation_order_number', 'customer_name', 'shipstation_order_id')
    inlines = [OrderItemInline]
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ProductMapping)
class ProductMappingAdmin(admin.ModelAdmin):
    list_display = ('sub_user', 'marketplace_sku', 'marketplace_variant', 'pc_variant')
    search_fields = ('marketplace_sku', 'marketplace_variant', 'sub_user__user__email')
