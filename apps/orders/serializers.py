from rest_framework import serializers

from apps.catalog.models import ProductVariant

from .models import Order, OrderItem, Store, UserProductMapping


class StoreSerializer(serializers.ModelSerializer):
    class Meta:
        model = Store
        fields = ('id', 'name', 'shipstation_store_id', 'is_active')


class OrderItemSerializer(serializers.ModelSerializer):
    catalog_variant_sku = serializers.CharField(source='catalog_variant.sku', read_only=True)

    class Meta:
        model = OrderItem
        fields = (
            'id', 'raw_product_name', 'raw_variant', 'quantity',
            'personalization', 'catalog_variant', 'catalog_variant_sku', 'matched_at'
        )


class OrderListSerializer(serializers.ModelSerializer):
    store_name = serializers.CharField(source='store.name', read_only=True)
    items_count = serializers.IntegerField(source='items.count', read_only=True)
    is_matched = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            'id', 'external_order_number', 'customer_name', 'store_name',
            'ship_by_date', 'ordered_at', 'requested_service', 'status',
            'items_count', 'is_matched'
        )

    def get_is_matched(self, obj):
        return not obj.items.filter(catalog_variant__isnull=True).exists()


class OrderDetailSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    store_name = serializers.CharField(source='store.name', read_only=True)

    class Meta:
        model = Order
        fields = (
            'id', 'external_order_number', 'shipstation_order_id',
            'customer_name', 'customer_email', 'store', 'store_name',
            'ship_to_name', 'ship_to_address1', 'ship_to_address2',
            'ship_to_city', 'ship_to_state', 'ship_to_postal_code',
            'ship_to_country', 'address_verified',
            'ship_by_date', 'ordered_at',
            'requested_service', 'selected_service', 'shipping_cost',
            'buyer_note', 'print_note', 'status', 'items',
        )


class AddressUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = (
            'ship_to_name', 'ship_to_address1', 'ship_to_address2',
            'ship_to_city', 'ship_to_state', 'ship_to_postal_code', 'ship_to_country',
        )


class MatchItemSerializer(serializers.Serializer):
    item_id = serializers.IntegerField()
    catalog_variant_id = serializers.IntegerField()
    remember = serializers.BooleanField(default=False)

    def validate_catalog_variant_id(self, value):
        if not ProductVariant.objects.filter(id=value).exists():
            raise serializers.ValidationError('Variant not found')
        return value


class UserProductMappingSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProductMapping
        fields = ('id', 'etsy_variant_name', 'catalog_variant')
