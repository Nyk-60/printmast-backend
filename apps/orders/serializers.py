from rest_framework import serializers

from .models import Order, OrderItem, ProductMapping


class OrderItemSerializer(serializers.ModelSerializer):
    pc_variant_sku     = serializers.CharField(source='pc_variant.pc_product.sku', read_only=True)
    pc_variant_display = serializers.SerializerMethodField()
    is_matched         = serializers.SerializerMethodField()
    pc_product_name    = serializers.CharField(source='pc_variant.pc_product.name', read_only=True, default=None)
    pc_variant_size    = serializers.CharField(source='pc_variant.size', read_only=True, default=None)
    pc_variant_color   = serializers.CharField(source='pc_variant.color', read_only=True, default=None)
    pc_variant1_label  = serializers.CharField(source='pc_variant.pc_product.variant1_label', read_only=True, default=None)
    pc_variant2_label  = serializers.CharField(source='pc_variant.pc_product.variant2_label', read_only=True, default=None)
    pc_variant_weight_oz = serializers.FloatField(source='pc_variant.weight_oz', read_only=True, default=None)
    display_image_url  = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = (
            'id', 'shipstation_item_id', 'marketplace_sku',
            'raw_product_name', 'raw_options', 'personalization',
            'quantity', 'unit_price', 'image_url', 'display_image_url',
            'pc_variant', 'pc_variant_sku', 'pc_variant_display', 'matched_at',
            'is_matched', 'pc_product_name', 'pc_variant_size', 'pc_variant_color',
            'pc_variant1_label', 'pc_variant2_label', 'pc_variant_weight_oz',
        )
        read_only_fields = ('id', 'matched_at')

    def get_pc_variant_display(self, obj):
        if obj.pc_variant:
            v = obj.pc_variant
            return f'{v.size} / {v.color}'.strip(' /')
        return None

    def get_is_matched(self, obj):
        return obj.pc_variant_id is not None

    def get_display_image_url(self, obj):
        if obj.custom_image:
            request = self.context.get('request')
            return request.build_absolute_uri(obj.custom_image.url) if request else obj.custom_image.url
        return obj.image_url


class OrderItemPreviewSerializer(serializers.ModelSerializer):
    is_matched           = serializers.SerializerMethodField()
    pc_product_name      = serializers.CharField(source='pc_variant.pc_product.name', read_only=True, default=None)
    pc_variant_size      = serializers.CharField(source='pc_variant.size', read_only=True, default=None)
    pc_variant_color     = serializers.CharField(source='pc_variant.color', read_only=True, default=None)
    pc_variant1_label    = serializers.CharField(source='pc_variant.pc_product.variant1_label', read_only=True, default=None)
    pc_variant2_label    = serializers.CharField(source='pc_variant.pc_product.variant2_label', read_only=True, default=None)
    pc_variant_weight_oz = serializers.FloatField(source='pc_variant.weight_oz', read_only=True, default=None)
    pc_variant_is_dark   = serializers.BooleanField(source='pc_variant.is_dark', read_only=True, default=None)
    display_image_url    = serializers.SerializerMethodField()
    designs               = serializers.SerializerMethodField()
    available_print_areas = serializers.SerializerMethodField()
    pc_product_colors     = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = (
            'id', 'image_url', 'display_image_url', 'raw_product_name', 'marketplace_sku',
            'raw_options', 'personalization', 'quantity', 'unit_price',
            'is_matched', 'pc_product_name', 'pc_variant_size', 'pc_variant_color',
            'pc_variant1_label', 'pc_variant2_label', 'pc_variant_weight_oz', 'pc_variant_is_dark',
            'designs', 'available_print_areas', 'pc_product_colors',
        )

    def get_is_matched(self, obj):
        return obj.pc_variant_id is not None

    def get_display_image_url(self, obj):
        if obj.custom_image:
            request = self.context.get('request')
            return request.build_absolute_uri(obj.custom_image.url) if request else obj.custom_image.url
        return obj.image_url

    def get_designs(self, obj):
        """Return all SkuDesigns for this SKU (library view), with is_matched flag.
        Falls back to OrderItemDesigns when item is unmatched or has orphaned designs."""
        from apps.designs.models import SkuDesign

        request = self.context.get('request')
        sub_user = getattr(request.user, 'sub_user_profile', None) if request else None
        if not sub_user:
            return []

        sku_designs = list(
            SkuDesign.objects.filter(sub_user=sub_user, sku=obj.marketplace_sku)
            .order_by('print_area_name', 'color_scope')
        )

        # Determine which design matches this item per print area
        matched_ids = set()
        if obj.pc_variant_id:
            from apps.orders.services import _find_sku_design
            variant = obj.pc_variant
            areas = variant.pc_product.print_areas.filter(is_active=True).values_list('name', flat=True)
            for area_name in areas:
                match = _find_sku_design(
                    sub_user, obj.marketplace_sku, area_name,
                    variant.color or '', variant.is_dark,
                    obj.personalization or '',
                )
                if match:
                    matched_ids.add(match.pk)

        result = []
        for sd in sku_designs:
            file_url = request.build_absolute_uri(sd.file.url) if (request and sd.file) else (sd.file.url if sd.file else None)
            result.append({
                'id':             sd.pk,
                'print_area_name': sd.print_area_name,
                'file':           file_url,
                'file_name':      sd.file_name,
                'color_scope':    sd.color_scope,
                'specific_color': sd.specific_color,
                'is_matched':     sd.pk in matched_ids,
            })

        # Matched designs first within each print area
        result.sort(key=lambda d: (d['print_area_name'], not d['is_matched']))
        return result

    def get_available_print_areas(self, obj):
        if not obj.pc_variant_id or not obj.pc_variant.pc_product_id:
            return []
        request = self.context.get('request')
        areas = []
        for area in obj.pc_variant.pc_product.print_areas.filter(is_active=True):
            icon_url = None
            if area.icon:
                icon_url = request.build_absolute_uri(area.icon.url) if request else area.icon.url
            areas.append({'name': area.name, 'is_default': area.is_default, 'icon': icon_url})
        return areas

    def get_pc_product_colors(self, obj):
        if not obj.pc_variant_id or not obj.pc_variant.pc_product_id:
            return []
        seen = set()
        colors = []
        for v in obj.pc_variant.pc_product.variants.filter(is_active=True).order_by('color'):
            if v.color and v.color not in seen:
                seen.add(v.color)
                colors.append({'name': v.color, 'hex': v.color_hex or '', 'is_dark': v.is_dark})
        return colors


class OrderCostMixin:
    """Shared cost-breakdown calculation for the order list and detail serializers."""

    def _billable_items(self, obj):
        return [i for i in obj.items.all() if i.pc_variant_id and float(i.unit_price) >= 0]

    def get_item_cost(self, obj):
        total = sum(float(i.pc_variant.cost) * i.quantity for i in self._billable_items(obj))
        return round(total, 2)

    def get_extra_print_area_fee(self, obj):
        sub_user = obj.sub_user
        if not sub_user or not sub_user.extra_print_area_enabled:
            return 0
        price = float(sub_user.extra_print_area_price or 0)
        if price <= 0:
            return 0
        extra_units = 0
        for item in self._billable_items(obj):
            extra_designs = max(0, item.designs.count() - 1)
            extra_units += extra_designs * item.quantity
        return round(extra_units * price, 2)


class OrderListSerializer(OrderCostMixin, serializers.ModelSerializer):
    store_name      = serializers.CharField(source='store.name', read_only=True)
    sub_user_name   = serializers.SerializerMethodField()
    unmatched_count = serializers.SerializerMethodField()
    has_design      = serializers.SerializerMethodField()
    items           = OrderItemPreviewSerializer(many=True, read_only=True)
    shipping_dims_auto    = serializers.SerializerMethodField()
    default_shipping_option = serializers.SerializerMethodField()
    selected_service_name   = serializers.SerializerMethodField()
    item_cost             = serializers.SerializerMethodField()
    extra_print_area_fee  = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            'id', 'shipstation_order_number', 'external_order_number',
            'customer_name', 'store', 'store_name', 'sub_user_name',
            'ship_to_name', 'ship_to_company',
            'ship_to_address1', 'ship_to_address2',
            'ship_to_city', 'ship_to_state', 'ship_to_postal_code', 'ship_to_country',
            'address_verified',
            'ship_by_date', 'ordered_at',
            'requested_service', 'shipping_cost', 'status',
            'selected_carrier_code', 'selected_service_code', 'selected_service_name',
            'buyer_note', 'gift_message', 'is_gift', 'internal_note',
            'tracking_number', 'label_url',
            'shipping_override', 'shipping_dims_auto', 'default_shipping_option',
            'unmatched_count', 'has_design', 'items',
            'item_cost', 'extra_print_area_fee',
        )

    def get_sub_user_name(self, obj):
        if not obj.sub_user_id:
            return None
        user = obj.sub_user.user
        return user.full_name or user.email

    def get_selected_service_name(self, obj):
        """Friendly display name for the selected carrier/service, as configured
        by the print center in Shipping Settings — falls back to a formatted code."""
        if not obj.selected_carrier_code or not obj.selected_service_code:
            return None

        from apps.shipping.models import ShippingServiceOption

        cache = self.context.setdefault('_service_name_cache', {})
        key = (obj.store.shipstation_account.print_center_id, obj.selected_carrier_code, obj.selected_service_code)
        if key not in cache:
            opt = ShippingServiceOption.objects.filter(
                print_center_id=key[0], carrier_code=key[1], service_code=key[2],
            ).first()
            cache[key] = opt.service_name if opt and opt.service_name else None

        return cache[key] or obj.selected_service_code.replace('_', ' ').title()

    def get_default_shipping_option(self, obj):
        """The sub-user's configured default shipping option for this order's
        requested-service category — shown as a preview before rates are fetched."""
        from apps.shipping.models import ShippingServiceDefault
        from apps.shipping.utils import categorize_requested_service

        if not obj.sub_user_id:
            return None

        category = categorize_requested_service(obj.requested_service)
        cache = self.context.setdefault('_default_shipping_cache', {})
        key = (obj.sub_user_id, category)
        if key not in cache:
            default = ShippingServiceDefault.objects.filter(
                sub_user_id=obj.sub_user_id, category=category, service_option__isnull=False,
            ).select_related('service_option').first()
            cache[key] = default.service_option if default else None

        opt = cache[key]
        if not opt:
            return None
        return {
            'carrier_code': opt.carrier_code,
            'service_code': opt.service_code,
            'service_name': opt.service_name,
        }

    def get_shipping_dims_auto(self, obj):
        try:
            from apps.shipping.utils import calculate_order_dimensions
            billable = [i for i in obj.items.all() if float(i.unit_price) >= 0 and i.pc_variant_id]
            weight_oz = sum(float(i.pc_variant.weight_oz or 0) * i.quantity for i in billable)
            dims = calculate_order_dimensions(obj)
            return {'weight_oz': round(weight_oz, 2), 'length': dims['length'],
                    'width': dims['width'], 'height': dims['height']}
        except Exception:
            return None

    def get_unmatched_count(self, obj):
        return sum(1 for i in obj.items.all() if i.pc_variant_id is None and i.unit_price >= 0)

    def get_has_design(self, obj):
        return getattr(obj, '_has_design', False)


class OrderDetailSerializer(OrderCostMixin, serializers.ModelSerializer):
    items       = OrderItemPreviewSerializer(many=True, read_only=True)
    store_name  = serializers.CharField(source='store.name', read_only=True)
    marketplace = serializers.CharField(source='store.marketplace', read_only=True)
    shipping_dims_auto   = serializers.SerializerMethodField()
    item_cost            = serializers.SerializerMethodField()
    extra_print_area_fee = serializers.SerializerMethodField()

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
            'buyer_note', 'print_note', 'status',
            'shipping_override', 'shipping_dims_auto',
            'items', 'item_cost', 'extra_print_area_fee',
        )

    def get_shipping_dims_auto(self, obj):
        """Auto-calculated weight + dims (always shown as reference, even when override is set)."""
        try:
            from apps.shipping.utils import calculate_order_dimensions
            billable = [i for i in obj.items.all() if float(i.unit_price) >= 0 and i.pc_variant_id]
            weight_oz = sum(float(i.pc_variant.weight_oz or 0) * i.quantity for i in billable)
            dims = calculate_order_dimensions(obj)
            return {
                'weight_oz': round(weight_oz, 2),
                'length':    dims['length'],
                'width':     dims['width'],
                'height':    dims['height'],
            }
        except Exception:
            return None


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


class ShippingOverrideSerializer(serializers.Serializer):
    weight_oz = serializers.DecimalField(max_digits=8,  decimal_places=2, required=False, allow_null=True)
    length    = serializers.DecimalField(max_digits=6,  decimal_places=2, required=False, allow_null=True)
    width     = serializers.DecimalField(max_digits=6,  decimal_places=2, required=False, allow_null=True)
    height    = serializers.DecimalField(max_digits=6,  decimal_places=2, required=False, allow_null=True)
    note      = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')
    clear     = serializers.BooleanField(required=False, default=False)


class ProductMappingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductMapping
        fields = ('id', 'store', 'marketplace_sku', 'marketplace_variant', 'pc_variant')
