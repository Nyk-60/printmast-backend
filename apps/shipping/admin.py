from django.contrib import admin

from .models import ShippingRateMarkup, ShipStationCarrier


@admin.register(ShippingRateMarkup)
class ShippingRateMarkupAdmin(admin.ModelAdmin):
    list_display = ('service', 'markup_flat', 'markup_percent')


admin.site.register(ShipStationCarrier)
