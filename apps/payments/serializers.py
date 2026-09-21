from decimal import Decimal

from rest_framework import serializers

from apps.accounts.models import BalanceTransaction

from .models import Transaction


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

    class Meta:
        model = BalanceTransaction
        fields = (
            'id', 'type', 'amount', 'item_amount', 'shipping_amount',
            'description', 'order_reference', 'created_by_email', 'created_at',
        )
        read_only_fields = fields


class BalanceSummarySerializer(serializers.Serializer):
    current_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_credited = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_debited = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_refunded = serializers.DecimalField(max_digits=12, decimal_places=2)
    this_month_credited = serializers.DecimalField(max_digits=12, decimal_places=2)
    this_month_debited = serializers.DecimalField(max_digits=12, decimal_places=2)
    transaction_count = serializers.IntegerField()


class SubUserBalanceSerializer(serializers.Serializer):
    sub_user_id = serializers.IntegerField()
    email = serializers.EmailField()
    full_name = serializers.CharField()
    balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    negative_balance_limit = serializers.DecimalField(max_digits=12, decimal_places=2)
    is_active = serializers.BooleanField()
    total_credited = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_debited = serializers.DecimalField(max_digits=12, decimal_places=2)
