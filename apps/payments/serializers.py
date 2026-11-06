from decimal import Decimal
from zoneinfo import ZoneInfo

from rest_framework import serializers

from apps.accounts.models import BalanceTransaction

from .models import Transaction


def localize(dt, tz_name):
    try:
        return dt.astimezone(ZoneInfo(tz_name)).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return dt.strftime('%Y-%m-%d %H:%M')  # bad/unset tz name — fall back to UTC


def pc_time(tx):
    return localize(tx.created_at, tx.sub_user.print_center.timezone)


def user_time(tx):
    return localize(tx.created_at, tx.sub_user.timezone)


def user_label(tx):
    user = tx.sub_user.user
    return user.full_name or user.email


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = ('id', 'tx_type', 'amount', 'status', 'reference', 'note', 'created_at')
        read_only_fields = ('id', 'created_at', 'status')


class AddFundsSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal('1')
    )
    payment_method = serializers.CharField(required=False, allow_blank=True)


class BalanceTransactionSerializer(serializers.ModelSerializer):
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    display_id = serializers.IntegerField(read_only=True)
    shop = serializers.SerializerMethodField()
    user_label = serializers.SerializerMethodField()
    pc_local_time = serializers.SerializerMethodField()
    user_local_time = serializers.SerializerMethodField()
    balance_after = serializers.SerializerMethodField()

    class Meta:
        model = BalanceTransaction
        fields = (
            'id', 'display_id', 'type', 'category', 'category_display', 'amount',
            'item_amount', 'shipping_amount', 'product_name', 'size', 'color', 'quantity',
            'description', 'order_reference', 'order_number', 'shop',
            'user_label', 'pc_local_time', 'user_local_time', 'balance_after',
            'created_by_email', 'created_at',
        )
        read_only_fields = fields

    def get_shop(self, obj):
        return obj.order.store.name if obj.order_id and obj.order.store_id else ''

    def get_user_label(self, obj):
        return user_label(obj)

    def get_pc_local_time(self, obj):
        return pc_time(obj)

    def get_user_local_time(self, obj):
        return user_time(obj)

    def get_balance_after(self, obj):
        val = getattr(obj, 'balance_after', None)
        return str(val) if val is not None else None


class BalanceSummarySerializer(serializers.Serializer):
    current_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_credited = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_debited = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_refunded = serializers.DecimalField(max_digits=12, decimal_places=2)
    this_month_credited = serializers.DecimalField(max_digits=12, decimal_places=2)
    this_month_debited = serializers.DecimalField(max_digits=12, decimal_places=2)
    transaction_count = serializers.IntegerField()
    unpaid_invoice_count = serializers.IntegerField()


class SubUserBalanceSerializer(serializers.Serializer):
    sub_user_id = serializers.IntegerField()
    email = serializers.EmailField()
    full_name = serializers.CharField()
    balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    negative_balance_limit = serializers.DecimalField(max_digits=12, decimal_places=2)
    is_active = serializers.BooleanField()
    total_credited = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_debited = serializers.DecimalField(max_digits=12, decimal_places=2)
