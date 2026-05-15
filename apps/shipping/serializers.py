from rest_framework import serializers

from .models import ShipStationAccount, Store


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
    assigned_sub_user_ids = serializers.PrimaryKeyRelatedField(
        source='assigned_sub_users', many=True, read_only=True
    )

    class Meta:
        model = Store
        fields = (
            'id', 'shipstation_store_id', 'name', 'marketplace',
            'marketplace_url', 'is_active', 'account_label', 'assigned_sub_user_ids',
        )
        read_only_fields = ('id', 'shipstation_store_id', 'name', 'marketplace',
                            'marketplace_url', 'account_label', 'assigned_sub_user_ids')


class AssignStoreSerializer(serializers.Serializer):
    sub_user_ids = serializers.ListField(
        child=serializers.IntegerField(),
        help_text='List of SubUserProfile IDs to assign. Replaces current assignments.',
    )


class RateRequestSerializer(serializers.Serializer):
    ship_to_country = serializers.CharField(max_length=2)
    ship_to_postal_code = serializers.CharField(max_length=20)
    weight_oz = serializers.FloatField()
    carrier_code = serializers.CharField(required=False, allow_blank=True, default='')
