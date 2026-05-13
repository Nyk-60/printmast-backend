from rest_framework import serializers

from .models import Transaction


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = ('id', 'tx_type', 'amount', 'status', 'reference', 'note', 'created_at')
        read_only_fields = ('id', 'created_at', 'status')


class AddFundsSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=1)
    payment_method = serializers.CharField(required=False, allow_blank=True)
