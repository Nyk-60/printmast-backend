from django.contrib import admin

from .models import BatchOrder, GangSheet, OrderSlipPDF, ProductionBatch


class BatchOrderInline(admin.TabularInline):
    model = BatchOrder
    extra = 0
    fields = ('sequence_number', 'order', 'label_purchased', 'label_cost')
    readonly_fields = ('label_purchased', 'label_cost')


class GangSheetInline(admin.TabularInline):
    model = GangSheet
    extra = 0
    fields = ('sheet_number', 'file', 'width_inches', 'length_inches')
    readonly_fields = fields


class OrderSlipPDFInline(admin.TabularInline):
    model = OrderSlipPDF
    extra = 0
    fields = ('gang_sheet', 'file')
    readonly_fields = fields


@admin.register(ProductionBatch)
class ProductionBatchAdmin(admin.ModelAdmin):
    list_display = ('id', 'print_center', 'status', 'sheet_length_inches', 'created_at')
    list_filter = ('status', 'print_center')
    readonly_fields = ('status', 'error_message', 'created_at', 'updated_at')
    inlines = [BatchOrderInline, GangSheetInline, OrderSlipPDFInline]
