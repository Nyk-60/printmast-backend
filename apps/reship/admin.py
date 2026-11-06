from django.contrib import admin

from .models import ReshipRequest, ReshipRequestFile, ReshipRequestItem


class ReshipRequestItemInline(admin.TabularInline):
    model = ReshipRequestItem
    extra = 0


class ReshipRequestFileInline(admin.TabularInline):
    model = ReshipRequestFile
    extra = 0


@admin.register(ReshipRequest)
class ReshipRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'original_order', 'sub_user', 'fault_type', 'status', 'created_at')
    list_filter = ('fault_type', 'status')
    search_fields = ('original_order__shipstation_order_number', 'original_order__customer_name')
    inlines = (ReshipRequestItemInline, ReshipRequestFileInline)
