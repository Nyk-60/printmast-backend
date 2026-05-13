from django.contrib import admin

from .models import Order, OrderItem, Store, UserProductMapping


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('external_order_number', 'customer_name', 'store', 'status', 'ship_by_date', 'ordered_at')
    list_filter = ('status', 'store')
    search_fields = ('external_order_number', 'customer_name', 'shipstation_order_id')
    inlines = [OrderItemInline]


admin.site.register(Store)
admin.site.register(UserProductMapping)
