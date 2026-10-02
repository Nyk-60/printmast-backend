from rest_framework import serializers

from .models import BatchOrder, GangSheet, OrderSlipPDF, ProductionBatch


class GangSheetSerializer(serializers.ModelSerializer):
    class Meta:
        model = GangSheet
        fields = ('id', 'sheet_number', 'file', 'width_inches', 'length_inches')
        read_only_fields = fields


class OrderSlipPDFSerializer(serializers.ModelSerializer):
    sheet_number = serializers.IntegerField(source='gang_sheet.sheet_number', read_only=True)

    class Meta:
        model = OrderSlipPDF
        fields = ('id', 'sheet_number', 'file')
        read_only_fields = fields


class BatchOrderSerializer(serializers.ModelSerializer):
    order_number = serializers.CharField(source='order.shipstation_order_number', read_only=True)
    customer = serializers.CharField(source='order.customer_name', read_only=True)
    status = serializers.CharField(source='order.status', read_only=True)
    store_name = serializers.CharField(source='order.store.name', read_only=True)
    tracking_number = serializers.CharField(source='order.tracking_number', read_only=True)
    address_status = serializers.CharField(source='order.address_status', read_only=True)
    ship_to = serializers.SerializerMethodField()

    class Meta:
        model = BatchOrder
        fields = ('id', 'sequence_number', 'order', 'order_number', 'customer', 'status',
                  'store_name', 'label_status', 'error_kind', 'error_message',
                  'label_purchased', 'label_cost', 'tracking_number', 'address_status', 'ship_to')
        read_only_fields = fields

    def get_ship_to(self, obj):
        o = obj.order
        return {
            'ship_to_name': o.ship_to_name, 'ship_to_company': o.ship_to_company,
            'ship_to_address1': o.ship_to_address1, 'ship_to_address2': o.ship_to_address2,
            'ship_to_city': o.ship_to_city, 'ship_to_state': o.ship_to_state,
            'ship_to_postal_code': o.ship_to_postal_code, 'ship_to_country': o.ship_to_country,
        }


def _label_counts(batch):
    """One grouped query instead of loading every order of the batch."""
    from django.db.models import Count

    counts = {s.value: 0 for s in BatchOrder.LabelStatus}
    for row in batch.batch_orders.values('label_status').annotate(c=Count('id')):
        counts[row['label_status']] = row['c']
    counts['total'] = sum(v for k, v in counts.items() if k != 'removed')
    return counts


class ProductionBatchListSerializer(serializers.ModelSerializer):
    """Lightweight row for the batch list — no per-order data."""
    counts = serializers.SerializerMethodField()

    class Meta:
        model = ProductionBatch
        fields = ('id', 'name', 'status', 'sheet_length_inches', 'note', 'error_message',
                  'paused_reason', 'counts', 'created_at')
        read_only_fields = fields

    def get_counts(self, obj):
        return _label_counts(obj)


class ProductionBatchSerializer(serializers.ModelSerializer):
    batch_orders = BatchOrderSerializer(many=True, read_only=True)
    gang_sheets = GangSheetSerializer(many=True, read_only=True)
    slip_pdfs = OrderSlipPDFSerializer(many=True, read_only=True)
    counts = serializers.SerializerMethodField()

    class Meta:
        model = ProductionBatch
        fields = (
            'id', 'name', 'status', 'sheet_length_inches', 'gang_sheet_dpi',
            'margin_inches', 'note', 'error_message', 'paused_reason', 'counts',
            'batch_orders', 'gang_sheets', 'slip_pdfs', 'created_at',
        )
        read_only_fields = ('id', 'status', 'error_message', 'paused_reason', 'counts', 'gang_sheets',
                            'slip_pdfs', 'batch_orders', 'created_at')

    def get_counts(self, obj):
        return _label_counts(obj)


class CreateBatchSerializer(serializers.Serializer):
    order_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)
    name = serializers.CharField(max_length=120, required=False, allow_blank=True, default='')
    sheet_length_inches = serializers.ChoiceField(
        choices=[100, 200, 250, 300, 350, 400], default=200
    )
    gang_sheet_dpi = serializers.IntegerField(default=300, min_value=150, max_value=600)
    margin_inches = serializers.DecimalField(max_digits=4, decimal_places=2, default='0.20')
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')
    skip_invalid = serializers.BooleanField(
        default=False,
        help_text='Leave orders that fail the pre-flight checks out of the batch instead of refusing it.',
    )


class PreflightSerializer(serializers.Serializer):
    order_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)
    sheet_length_inches = serializers.ChoiceField(choices=[100, 200, 250, 300, 350, 400], default=200)
    margin_inches = serializers.DecimalField(max_digits=4, decimal_places=2, default='0.20')
