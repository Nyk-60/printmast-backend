from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import BalanceTransaction, PrintCenterProfile, ShippingAutoRule, SubUserProfile, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ('email', 'full_name', 'role', 'is_active', 'is_staff', 'date_joined')
    list_filter = ('role', 'is_active', 'is_staff')
    search_fields = ('email', 'full_name')
    ordering = ('email',)
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Profile', {'fields': ('full_name', 'role')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'password1', 'password2', 'full_name', 'role'),
        }),
    )


@admin.register(PrintCenterProfile)
class PrintCenterProfileAdmin(admin.ModelAdmin):
    list_display = ('company_name', 'user', 'status', 'max_sub_users', 'timezone', 'created_at')
    list_filter = ('status',)
    search_fields = ('company_name', 'user__email')
    readonly_fields = ('created_at', 'updated_at')
    actions = ['activate', 'suspend']

    @admin.action(description='Activate selected print centers')
    def activate(self, request, queryset):
        for pc in queryset:
            pc.status = PrintCenterProfile.Status.ACTIVE
            pc.user.is_active = True
            pc.user.save(update_fields=['is_active'])
            pc.save(update_fields=['status'])

    @admin.action(description='Suspend selected print centers')
    def suspend(self, request, queryset):
        for pc in queryset:
            pc.status = PrintCenterProfile.Status.SUSPENDED
            pc.user.is_active = False
            pc.user.save(update_fields=['is_active'])
            pc.save(update_fields=['status'])


@admin.register(SubUserProfile)
class SubUserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'print_center', 'is_active', 'balance', 'created_at')
    list_filter = ('is_active', 'print_center')
    search_fields = ('user__email', 'print_center__company_name')
    readonly_fields = ('balance', 'created_at', 'updated_at')
    fieldsets = (
        (None, {'fields': ('user', 'print_center', 'created_by', 'is_active')}),
        ('Balance', {'fields': ('balance', 'negative_balance_limit', 'credit_until')}),
        ('Shipping Markup', {'fields': ('shipping_markup_type', 'shipping_markup_value')}),
        ('Print Area Pricing', {'fields': ('extra_print_area_enabled', 'extra_print_area_price')}),
        ('Permissions', {'fields': (
            'can_edit_orders', 'can_upload_product_mapping', 'can_upload_designs',
            'can_send_to_print', 'can_track_orders', 'can_view_financials', 'can_export_financials',
            'can_reship_without_pc_approval', 'can_report_lost_shipment',
        )}),
        ('Auto-matching', {'fields': ('auto_product_matching', 'auto_design_matching', 'auto_shipping_rules')}),
    )


@admin.register(BalanceTransaction)
class BalanceTransactionAdmin(admin.ModelAdmin):
    list_display = ('sub_user', 'type', 'amount', 'order_reference', 'created_by', 'created_at')
    list_filter = ('type',)
    search_fields = ('sub_user__user__email', 'order_reference')
    readonly_fields = ('created_at',)


@admin.register(ShippingAutoRule)
class ShippingAutoRuleAdmin(admin.ModelAdmin):
    list_display = ('sub_user', 'marketplace_service', 'carrier_service')
    search_fields = ('sub_user__user__email', 'marketplace_service')
