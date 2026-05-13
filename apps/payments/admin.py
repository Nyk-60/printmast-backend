from django.contrib import admin

from .models import Transaction


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('owner', 'tx_type', 'amount', 'status', 'created_at')
    list_filter = ('tx_type', 'status')
    search_fields = ('owner__email', 'reference')
