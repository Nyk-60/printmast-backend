from decimal import Decimal

from django.contrib.auth import authenticate
from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    BalanceTransaction,
    ImpersonationLog,
    PrintCenterInvoice,
    PrintCenterPricingTier,
    PrintCenterProfile,
    ShippingAutoRule,
    SubUserProfile,
    User,
)


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


# ── SuperAdmin — Print Center Management ──────────────────────────────────────

class PrintCenterProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source='user.email', read_only=True)
    full_name = serializers.CharField(source='user.full_name', read_only=True)
    sub_user_count = serializers.SerializerMethodField()
    active_shop_count = serializers.SerializerMethodField()
    awaiting_order_count = serializers.SerializerMethodField()
    completed_order_count = serializers.SerializerMethodField()
    logo_url = serializers.SerializerMethodField()

    class Meta:
        model = PrintCenterProfile
        fields = (
            'id', 'email', 'full_name', 'company_name',
            'status', 'max_sub_users', 'sub_user_count',
            'timezone', 'monthly_fee', 'logo_url',
            'active_shop_count', 'awaiting_order_count', 'completed_order_count',
            'created_at',
            'ship_from_name', 'ship_from_phone',
            'ship_from_street1', 'ship_from_street2',
            'ship_from_city', 'ship_from_state',
            'ship_from_postal_code', 'ship_from_country',
        )
        read_only_fields = (
            'id', 'email', 'full_name', 'status', 'sub_user_count',
            'active_shop_count', 'awaiting_order_count', 'completed_order_count',
            'logo_url', 'created_at',
        )

    def get_sub_user_count(self, obj):
        return getattr(obj, 'sub_user_count_ann', None) or obj.sub_users.count()

    def get_active_shop_count(self, obj):
        return getattr(obj, 'active_shop_count', 0) or 0

    def get_awaiting_order_count(self, obj):
        return getattr(obj, 'awaiting_order_count', 0) or 0

    def get_completed_order_count(self, obj):
        return getattr(obj, 'completed_order_count', 0) or 0

    def get_logo_url(self, obj):
        if not obj.logo:
            return None
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(obj.logo.url)
        return obj.logo.url


class CreatePrintCenterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    full_name = serializers.CharField(max_length=120, required=False, default='')
    company_name = serializers.CharField(max_length=200)
    timezone = serializers.CharField(max_length=64, default='UTC')
    monthly_fee = serializers.DecimalField(max_digits=10, decimal_places=2, default=0)
    max_sub_users = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    logo = serializers.ImageField(required=False, allow_null=True)

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError('Email already registered')
        return value

    @transaction.atomic
    def create(self, validated_data):
        logo = validated_data.pop('logo', None)
        user = User.objects.create_user(
            email=validated_data['email'],
            password=validated_data['password'],
            full_name=validated_data.get('full_name', ''),
            role=User.Role.PRINT_CENTER,
        )
        profile = PrintCenterProfile.objects.create(
            user=user,
            company_name=validated_data['company_name'],
            timezone=validated_data.get('timezone', 'UTC'),
            monthly_fee=validated_data.get('monthly_fee', 0),
            max_sub_users=validated_data.get('max_sub_users'),
            status=PrintCenterProfile.Status.ACTIVE,
        )
        if logo:
            profile.logo = logo
            profile.save(update_fields=['logo'])
        return profile


class UpdatePrintCenterSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=PrintCenterProfile.Status.choices, required=False)
    company_name = serializers.CharField(max_length=200, required=False)
    timezone = serializers.CharField(max_length=64, required=False)
    monthly_fee = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    max_sub_users = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    logo = serializers.ImageField(required=False, allow_null=True)
    email = serializers.EmailField(required=False)
    password = serializers.CharField(write_only=True, min_length=8, required=False)


# ── Pricing Tiers ─────────────────────────────────────────────────────────────

class PrintCenterPricingTierSerializer(serializers.ModelSerializer):
    class Meta:
        model = PrintCenterPricingTier
        fields = ('id', 'min_items', 'max_items', 'extra_fee')
        read_only_fields = ('id',)


# ── Invoices ──────────────────────────────────────────────────────────────────

class PrintCenterInvoiceSerializer(serializers.ModelSerializer):
    print_center_name = serializers.CharField(source='print_center.company_name', read_only=True)

    class Meta:
        model = PrintCenterInvoice
        fields = (
            'id', 'print_center', 'print_center_name',
            'period_year', 'period_month', 'amount',
            'status', 'due_date', 'paid_at', 'note', 'created_at',
        )
        read_only_fields = ('id', 'print_center_name', 'created_at')


# ── Legacy register serializer (disabled at view level) ───────────────────────

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


# ── Sub User ──────────────────────────────────────────────────────────────────

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
    print_center_name = serializers.CharField(source='print_center.company_name', read_only=True)
    print_center_logo_url = serializers.SerializerMethodField()

    class Meta:
        model = SubUserProfile
        fields = (
            'id', 'user_id', 'email', 'full_name', 'is_active',
            'print_center_name', 'print_center_logo_url',
            'balance', 'negative_balance_limit', 'credit_until',
            'shipping_markup_type', 'shipping_markup_value',
            'extra_print_area_enabled', 'extra_print_area_price',
            'can_edit_orders', 'can_upload_product_mapping', 'can_upload_designs',
            'can_send_to_print', 'can_track_orders', 'can_view_financials', 'can_export_financials',
            'auto_product_matching', 'auto_design_matching', 'auto_shipping_rules',
            'created_at',
        )
        read_only_fields = ('id', 'user_id', 'email', 'balance', 'created_at',
                            'print_center_name', 'print_center_logo_url')

    def get_print_center_logo_url(self, obj):
        if not obj.print_center.logo:
            return None
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(obj.print_center.logo.url)
        return obj.print_center.logo.url

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
