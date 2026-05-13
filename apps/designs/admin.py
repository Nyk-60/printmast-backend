from django.contrib import admin

from .models import Design, OrderDesign, UserSkuDesign

admin.site.register(Design)
admin.site.register(UserSkuDesign)
admin.site.register(OrderDesign)
