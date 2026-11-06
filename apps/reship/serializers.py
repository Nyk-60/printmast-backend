from rest_framework import serializers

from .models import ReshipRequest, ReshipRequestFile, ReshipRequestItem


class ReshipRequestFileSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = ReshipRequestFile
        fields = ('id', 'url', 'created_at')

    def get_url(self, obj):
        request = self.context.get('request')
        return request.build_absolute_uri(obj.file.url) if request else obj.file.url


class ReshipRequestItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='original_order_item.raw_product_name', read_only=True)
    size = serializers.CharField(source='original_order_item.pc_variant.size', read_only=True, default=None)
    color = serializers.CharField(source='original_order_item.pc_variant.color', read_only=True, default=None)
    original_quantity = serializers.IntegerField(source='original_order_item.quantity', read_only=True)
    reason_display = serializers.CharField(source='get_reason_display', read_only=True)
    original_personalization = serializers.CharField(
        source='original_order_item.personalization', read_only=True, default='',
    )

    class Meta:
        model = ReshipRequestItem
        fields = (
            'id', 'original_order_item', 'product_name', 'size', 'color',
            'original_quantity', 'quantity', 'reason', 'reason_display',
            'personalization_override', 'original_personalization',
        )
        read_only_fields = fields


class ReshipRequestSerializer(serializers.ModelSerializer):
    order_number = serializers.CharField(
        source='original_order.shipstation_order_number', read_only=True,
    )
    customer_name = serializers.CharField(source='original_order.customer_name', read_only=True)
    sub_user_name = serializers.SerializerMethodField()
    request_type_display = serializers.CharField(source='get_request_type_display', read_only=True)
    fault_type_display = serializers.CharField(source='get_fault_type_display', read_only=True)
    reason_display = serializers.CharField(source='get_reason_display', read_only=True)
    resolved_by_email = serializers.EmailField(source='resolved_by.email', read_only=True, default=None)
    new_order_number = serializers.CharField(
        source='new_order.shipstation_order_number', read_only=True, default=None,
    )
    items = ReshipRequestItemSerializer(many=True, read_only=True)
    files = ReshipRequestFileSerializer(many=True, read_only=True)

    class Meta:
        model = ReshipRequest
        fields = (
            'id', 'original_order', 'order_number', 'customer_name',
            'sub_user', 'sub_user_name', 'request_type', 'request_type_display',
            'fault_type', 'fault_type_display',
            'status', 'reason', 'reason_display', 'note', 'auto_approved',
            'address_override', 'new_order', 'new_order_number', 'resolved_at',
            'resolved_by_email', 'rejection_note', 'shipstation_synced',
            'shipstation_sync_error', 'refund_amount', 'items', 'files', 'created_at',
        )
        read_only_fields = fields

    def get_sub_user_name(self, obj):
        user = obj.sub_user.user
        return user.full_name or user.email
