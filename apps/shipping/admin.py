from django.contrib import admin

from .models import ShipStationAccount, ShipStationCarrier, Store


@admin.register(ShipStationAccount)
class ShipStationAccountAdmin(admin.ModelAdmin):
    list_display = ('label', 'print_center', 'api_version', 'is_active', 'last_synced_at')
    list_filter = ('api_version', 'is_active', 'print_center')
    search_fields = ('label', 'print_center__company_name')
    readonly_fields = ('last_synced_at', 'created_at', 'updated_at')


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ('name', 'marketplace', 'shipstation_account', 'is_active')
    list_filter = ('marketplace', 'is_active', 'shipstation_account__print_center')
    search_fields = ('name', 'shipstation_store_id')
    filter_horizontal = ('assigned_sub_users',)


admin.site.register(ShipStationCarrier)
