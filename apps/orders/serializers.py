from rest_framework import serializers

from .models import Order, OrderItem, ProductMapping


class OrderItemSerializer(serializers.ModelSerializer):
    pc_variant_sku = serializers.CharField(source='pc_variant.pc_product.sku', read_only=True)
    pc_variant_display = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = (
            'id', 'shipstation_item_id', 'marketplace_sku',
            'raw_product_name', 'raw_options', 'personalization',
            'quantity', 'unit_price', 'image_url',
            'pc_variant', 'pc_variant_sku', 'pc_variant_display', 'matched_at',
        )
        read_only_fields = ('id', 'matched_at')

    def get_pc_variant_display(self, obj):
        if obj.pc_variant:
            v = obj.pc_variant
            return f'{v.size} / {v.color}'.strip(' /')
        return None


class OrderListSerializer(serializers.ModelSerializer):
    store_name = serializers.CharField(source='store.name', read_only=True)
    items_count = serializers.IntegerField(source='items.count', read_only=True)
    unmatched_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            'id', 'shipstation_order_number', 'customer_name', 'store_name',
            'ship_by_date', 'ordered_at', 'requested_service', 'status',
            'items_count', 'unmatched_count',
        )

    def get_unmatched_count(self, obj):
        return obj.items.filter(pc_variant__isnull=True).count()


class OrderDetailSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    store_name = serializers.CharField(source='store.name', read_only=True)
    marketplace = serializers.CharField(source='store.marketplace', read_only=True)

    class Meta:
        model = Order
        fields = (
            'id', 'shipstation_order_number', 'shipstation_order_id',
            'external_order_number', 'customer_name', 'customer_email',
            'store', 'store_name', 'marketplace',
            'ship_to_name', 'ship_to_company',
            'ship_to_address1', 'ship_to_address2',
            'ship_to_city', 'ship_to_state', 'ship_to_postal_code', 'ship_to_country',
            'address_verified', 'ship_by_date', 'ordered_at',
            'requested_service', 'selected_carrier_code', 'selected_service_code',
            'shipping_cost', 'tracking_number', 'label_url',
            'buyer_note', 'print_note', 'status', 'items',
        )


class AddressUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = (
            'ship_to_name', 'ship_to_company',
            'ship_to_address1', 'ship_to_address2',
            'ship_to_city', 'ship_to_state', 'ship_to_postal_code', 'ship_to_country',
        )


class MatchItemSerializer(serializers.Serializer):
    item_id = serializers.IntegerField()
    pc_variant_id = serializers.IntegerField()
    remember = serializers.BooleanField(default=False)

    def validate_pc_variant_id(self, value):
        from apps.catalog.models import PrintCenterVariant
        if not PrintCenterVariant.objects.filter(id=value).exists():
            raise serializers.ValidationError('Variant not found')
        return value


class ProductMappingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductMapping
        fields = ('id', 'store', 'marketplace_sku', 'marketplace_variant', 'pc_variant')
