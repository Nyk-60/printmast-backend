from rest_framework import serializers

from .models import Design, OrderDesign, UserSkuDesign


class DesignSerializer(serializers.ModelSerializer):
    class Meta:
        model = Design
        fields = ('id', 'name', 'dark_file', 'light_file', 'created_at')
        read_only_fields = ('id', 'created_at')


class UserSkuDesignSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserSkuDesign
        fields = ('id', 'sku', 'print_area', 'dark_design', 'light_design', 'updated_at')


class OrderDesignSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderDesign
        fields = ('id', 'order_item', 'print_area', 'design_file', 'preview_image')
