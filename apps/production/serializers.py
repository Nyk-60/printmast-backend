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

    class Meta:
        model = BatchOrder
        fields = ('id', 'sequence_number', 'order', 'order_number', 'customer',
                  'status', 'label_purchased', 'label_cost')
        read_only_fields = fields


class ProductionBatchSerializer(serializers.ModelSerializer):
    batch_orders = BatchOrderSerializer(many=True, read_only=True)
    gang_sheets = GangSheetSerializer(many=True, read_only=True)
    slip_pdfs = OrderSlipPDFSerializer(many=True, read_only=True)

    class Meta:
        model = ProductionBatch
        fields = (
            'id', 'status', 'sheet_length_inches', 'gang_sheet_dpi',
            'margin_inches', 'note', 'error_message',
            'batch_orders', 'gang_sheets', 'slip_pdfs', 'created_at',
        )
        read_only_fields = ('id', 'status', 'error_message', 'gang_sheets',
                            'slip_pdfs', 'batch_orders', 'created_at')


class CreateBatchSerializer(serializers.Serializer):
    order_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)
    sheet_length_inches = serializers.ChoiceField(
        choices=[100, 200, 250, 300, 350, 400], default=200
    )
    gang_sheet_dpi = serializers.IntegerField(default=300, min_value=150, max_value=600)
    margin_inches = serializers.DecimalField(max_digits=4, decimal_places=2, default='0.20')
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')
