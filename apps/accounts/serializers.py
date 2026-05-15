from decimal import Decimal

from django.contrib.auth import authenticate
from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from .models import BalanceTransaction, PrintCenterProfile, ShippingAutoRule, SubUserProfile, User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('id', 'email', 'full_name', 'role', 'is_staff')
        read_only_fields = ('id', 'role', 'is_staff')


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get('request'),
            username=attrs['email'],
            password=attrs['password'],
        )
        if not user:
            raise serializers.ValidationError({'detail': 'Invalid credentials'})
        if not user.is_active:
            raise serializers.ValidationError({'detail': 'Account is inactive'})
        refresh = RefreshToken.for_user(user)
        return {
            'user': UserSerializer(user).data,
            'access': str(refresh.access_token),
            'refresh': str(refresh),
        }


class RegisterPrintCenterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    full_name = serializers.CharField(max_length=120)
    company_name = serializers.CharField(max_length=200)

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError('Email already registered')
        return value

    @transaction.atomic
    def create(self, validated_data):
        user = User.objects.create_user(
            email=validated_data['email'],
            password=validated_data['password'],
            full_name=validated_data['full_name'],
            role=User.Role.PRINT_CENTER,
        )
        PrintCenterProfile.objects.create(
            user=user,
            company_name=validated_data['company_name'],
            status=PrintCenterProfile.Status.PENDING,
        )
        return user


class PrintCenterProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source='user.email', read_only=True)
    full_name = serializers.CharField(source='user.full_name', read_only=True)
    sub_user_count = serializers.SerializerMethodField()

    class Meta:
        model = PrintCenterProfile
        fields = (
            'id', 'email', 'full_name', 'company_name',
            'status', 'max_sub_users', 'sub_user_count',
            'timezone', 'created_at',
        )
        read_only_fields = ('id', 'status', 'sub_user_count', 'created_at')

    def get_sub_user_count(self, obj):
        return obj.sub_users.count()


class PrintCenterStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=PrintCenterProfile.Status.choices, required=False)
    max_sub_users = serializers.IntegerField(min_value=1, required=False, allow_null=True)


class SubUserCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    full_name = serializers.CharField(max_length=120, required=False, default='')

    # Permissions
    can_edit_orders = serializers.BooleanField(default=True)
    can_upload_product_mapping = serializers.BooleanField(default=True)
    can_upload_designs = serializers.BooleanField(default=True)
    can_send_to_print = serializers.BooleanField(default=True)
    can_track_orders = serializers.BooleanField(default=True)
    can_view_financials = serializers.BooleanField(default=True)
    can_export_financials = serializers.BooleanField(default=True)

    # Balance settings
    negative_balance_limit = serializers.DecimalField(max_digits=12, decimal_places=2, default=0)
    credit_until = serializers.DateField(required=False, allow_null=True)

    # Shipping markup
    shipping_markup_type = serializers.ChoiceField(
        choices=[('fixed', 'Fixed'), ('percent', 'Percent')], default='fixed'
    )
    shipping_markup_value = serializers.DecimalField(max_digits=8, decimal_places=2, default=0)

    # Extra print area pricing
    extra_print_area_enabled = serializers.BooleanField(default=False)
    extra_print_area_price = serializers.DecimalField(max_digits=8, decimal_places=2, default=0)

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError('Email already registered')
        return value

    @transaction.atomic
    def create(self, validated_data):
        print_center = self.context['print_center']
        if not print_center.can_create_sub_user:
            raise serializers.ValidationError({'detail': 'Sub user limit reached'})

        user = User.objects.create_user(
            email=validated_data['email'],
            password=validated_data['password'],
            full_name=validated_data.get('full_name', ''),
            role=User.Role.SUB_USER,
        )
        SubUserProfile.objects.create(
            user=user,
            print_center=print_center,
            created_by=self.context['request'].user,
            negative_balance_limit=validated_data['negative_balance_limit'],
            credit_until=validated_data.get('credit_until'),
            shipping_markup_type=validated_data['shipping_markup_type'],
            shipping_markup_value=validated_data['shipping_markup_value'],
            extra_print_area_enabled=validated_data['extra_print_area_enabled'],
            extra_print_area_price=validated_data['extra_print_area_price'],
            can_edit_orders=validated_data['can_edit_orders'],
            can_upload_product_mapping=validated_data['can_upload_product_mapping'],
            can_upload_designs=validated_data['can_upload_designs'],
            can_send_to_print=validated_data['can_send_to_print'],
            can_track_orders=validated_data['can_track_orders'],
            can_view_financials=validated_data['can_view_financials'],
            can_export_financials=validated_data['can_export_financials'],
        )
        return user


class SubUserProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source='user.email', read_only=True)
    full_name = serializers.CharField(source='user.full_name')
    user_id = serializers.IntegerField(source='user.id', read_only=True)

    class Meta:
        model = SubUserProfile
        fields = (
            'id', 'user_id', 'email', 'full_name', 'is_active',
            'balance', 'negative_balance_limit', 'credit_until',
            'shipping_markup_type', 'shipping_markup_value',
            'extra_print_area_enabled', 'extra_print_area_price',
            'can_edit_orders', 'can_upload_product_mapping', 'can_upload_designs',
            'can_send_to_print', 'can_track_orders', 'can_view_financials', 'can_export_financials',
            'auto_product_matching', 'auto_design_matching', 'auto_shipping_rules',
            'created_at',
        )
        read_only_fields = ('id', 'user_id', 'email', 'balance', 'created_at')

    def update(self, instance, validated_data):
        user_data = validated_data.pop('user', {})
        if 'full_name' in user_data:
            instance.user.full_name = user_data['full_name']
            instance.user.save(update_fields=['full_name'])
        return super().update(instance, validated_data)


class AddBalanceSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'))
    description = serializers.CharField(max_length=500, required=False, default='')


class BalanceTransactionSerializer(serializers.ModelSerializer):
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)

    class Meta:
        model = BalanceTransaction
        fields = (
            'id', 'type', 'amount', 'item_amount', 'shipping_amount',
            'description', 'order_reference', 'created_by_email', 'created_at',
        )
        read_only_fields = fields


class ShippingAutoRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShippingAutoRule
        fields = ('id', 'marketplace_service', 'carrier_service')
