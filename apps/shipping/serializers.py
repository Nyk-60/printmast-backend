from decimal import Decimal

from rest_framework import serializers

from .models import (
    FixedShippingRate,
    LabelProviderAccount,
    ShipStationAccount,
    ShippingServiceDefault,
    ShippingServiceOption,
    Store,
)


class ShipStationAccountSerializer(serializers.ModelSerializer):
    store_count = serializers.IntegerField(source='stores.count', read_only=True)
    api_secret = serializers.CharField(write_only=True, required=False, allow_blank=True, default='')

    class Meta:
        model = ShipStationAccount
        fields = (
            'id', 'label', 'api_version', 'api_key', 'api_secret',
            'is_active', 'last_synced_at', 'store_count',
        )
        read_only_fields = ('id', 'last_synced_at', 'store_count')
        extra_kwargs = {'api_key': {'write_only': True}}

    def validate(self, attrs):
        if attrs.get('api_version') == ShipStationAccount.ApiVersion.V1:
            if not attrs.get('api_secret'):
                raise serializers.ValidationError({'api_secret': 'Required for V1 accounts'})
        return attrs

    def create(self, validated_data):
        return ShipStationAccount.objects.create(
            print_center=self.context['print_center'],
            **validated_data,
        )


class StoreSerializer(serializers.ModelSerializer):
    account_label = serializers.CharField(source='shipstation_account.label', read_only=True)
    shipstation_account_id = serializers.IntegerField(source='shipstation_account.id', read_only=True)
    assigned_sub_user_ids = serializers.PrimaryKeyRelatedField(
        source='assigned_sub_users', many=True, read_only=True
    )

    class Meta:
        model = Store
        fields = (
            'id', 'shipstation_store_id', 'name', 'marketplace',
            'marketplace_url', 'is_active', 'account_label', 'shipstation_account_id',
            'assigned_sub_user_ids',
            'ship_from_name', 'ship_from_company',
            'ship_from_street1', 'ship_from_street2', 'ship_from_city', 'ship_from_state',
            'ship_from_postal_code', 'ship_from_country', 'ship_from_phone',
            'ship_from_configured', 'ship_from_synced_at',
        )
        read_only_fields = ('id', 'shipstation_store_id', 'name', 'marketplace',
                            'marketplace_url', 'account_label', 'shipstation_account_id',
                            'assigned_sub_user_ids',
                            'ship_from_street1', 'ship_from_street2', 'ship_from_city', 'ship_from_state',
                            'ship_from_postal_code', 'ship_from_country', 'ship_from_phone',
                            'ship_from_configured', 'ship_from_synced_at')


class AssignStoreSerializer(serializers.Serializer):
    sub_user_ids = serializers.ListField(
        child=serializers.IntegerField(),
        help_text='List of SubUserProfile IDs to assign. Replaces current assignments.',
    )


class LabelProviderAccountSerializer(serializers.ModelSerializer):
    provider_display = serializers.CharField(source='get_provider_display', read_only=True)
    byelabel_client_secret = serializers.CharField(write_only=True, required=False, allow_blank=True, default='')

    class Meta:
        model = LabelProviderAccount
        fields = (
            'id', 'provider', 'provider_display', 'label', 'is_active',
            'shipstation_account', 'byelabel_client_id', 'byelabel_client_secret', 'byelabel_workspace_id',
        )
        read_only_fields = ('id', 'provider_display')
        extra_kwargs = {'byelabel_client_id': {'write_only': True}, 'byelabel_workspace_id': {'write_only': True}}

    def validate_shipstation_account(self, value):
        pc = self.context.get('print_center') or getattr(self.instance, 'print_center', None)
        if value and pc and value.print_center_id != pc.pk:
            raise serializers.ValidationError('Not one of your ShipStation accounts.')
        return value

    def validate(self, attrs):
        provider = attrs.get('provider', getattr(self.instance, 'provider', None))
        if provider == LabelProviderAccount.Provider.SHIPSTATION:
            account = attrs.get('shipstation_account', getattr(self.instance, 'shipstation_account', None))
            if not account:
                raise serializers.ValidationError({'shipstation_account': 'Required for ShipStation provider'})
        elif provider == LabelProviderAccount.Provider.BYELABEL:
            for field in ('byelabel_client_id', 'byelabel_workspace_id'):
                if not attrs.get(field, getattr(self.instance, field, None)):
                    raise serializers.ValidationError({field: 'Required for ByeLabel provider'})
        return attrs

    def create(self, validated_data):
        return LabelProviderAccount.objects.create(
            print_center=self.context['print_center'], **validated_data,
        )


class ShippingServiceOptionSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    provider_display = serializers.CharField(source='get_provider_display', read_only=True)

    class Meta:
        model = ShippingServiceOption
        fields = (
            'id', 'category', 'category_display', 'provider', 'provider_display',
            'carrier_code', 'service_code', 'service_name', 'sort_order', 'is_active',
        )
        read_only_fields = ('id', 'category_display', 'provider_display')


class ShippingServiceDefaultSerializer(serializers.ModelSerializer):
    service_option_detail = ShippingServiceOptionSerializer(source='service_option', read_only=True)

    class Meta:
        model = ShippingServiceDefault
        fields = ('category', 'service_option', 'service_option_detail')

    def validate_service_option(self, value):
        if value is None:
            return value
        sub_user = self.context['sub_user']
        if value.print_center_id != sub_user.print_center_id:
            raise serializers.ValidationError('Invalid service option.')
        return value


class FixedShippingRateSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source='get_category_display', read_only=True)

    class Meta:
        model = FixedShippingRate
        fields = ('id', 'category', 'category_display', 'min_weight_oz', 'max_weight_oz', 'price')
        read_only_fields = ('id', 'category_display')

    def validate(self, attrs):
        min_w = attrs.get('min_weight_oz', getattr(self.instance, 'min_weight_oz', None))
        max_w = attrs.get('max_weight_oz', getattr(self.instance, 'max_weight_oz', None))
        category = attrs.get('category', getattr(self.instance, 'category', None))

        if max_w is not None and min_w is not None and max_w <= min_w:
            raise serializers.ValidationError({'max_weight_oz': 'Must be greater than the minimum weight.'})

        pc = self.context['print_center']
        qs = FixedShippingRate.objects.filter(print_center=pc, category=category)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        for other in qs:
            other_max = other.max_weight_oz if other.max_weight_oz is not None else Decimal('Infinity')
            this_max = max_w if max_w is not None else Decimal('Infinity')
            if min_w < other_max and this_max > other.min_weight_oz:
                raise serializers.ValidationError(
                    f'Overlaps an existing bracket: {other.min_weight_oz}–{other.max_weight_oz or "∞"} oz.'
                )
        return attrs

    def create(self, validated_data):
        return FixedShippingRate.objects.create(print_center=self.context['print_center'], **validated_data)


class RateRequestSerializer(serializers.Serializer):
    ship_to_country = serializers.CharField(max_length=2)
    ship_to_postal_code = serializers.CharField(max_length=20)
    weight_oz = serializers.FloatField()
    order_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    carrier_code = serializers.CharField(required=False, allow_blank=True, default='')
