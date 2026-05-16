from rest_framework import serializers

from .models import ColorCategoryList, OrderItemDesign, SkuDesign


class SkuDesignSerializer(serializers.ModelSerializer):
    class Meta:
        model = SkuDesign
        fields = (
            'id', 'sku', 'print_area_name', 'color_scope', 'specific_color',
            'file', 'file_name', 'width_px', 'height_px', 'created_at',
        )
        read_only_fields = ('id', 'file_name', 'width_px', 'height_px', 'created_at')

    def validate(self, attrs):
        if attrs.get('color_scope') == SkuDesign.ColorScope.SPECIFIC and not attrs.get('specific_color'):
            raise serializers.ValidationError({'specific_color': 'Required when color_scope is specific'})
        return attrs

    def create(self, validated_data):
        file = validated_data.get('file')
        if file:
            validated_data['file_name'] = file.name
            w, h = _image_dimensions(file)
            validated_data['width_px'] = w
            validated_data['height_px'] = h
        return SkuDesign.objects.create(sub_user=self.context['sub_user'], **validated_data)

    def update(self, instance, validated_data):
        file = validated_data.get('file')
        if file:
            validated_data['file_name'] = file.name
            w, h = _image_dimensions(file)
            validated_data['width_px'] = w
            validated_data['height_px'] = h
        return super().update(instance, validated_data)


class OrderItemDesignSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItemDesign
        fields = (
            'id', 'print_area_name', 'file', 'file_name',
            'width_px', 'height_px', 'sku_design', 'created_at',
        )
        read_only_fields = ('id', 'file_name', 'width_px', 'height_px', 'sku_design', 'created_at')

    def create(self, validated_data):
        file = validated_data.get('file')
        if file:
            validated_data['file_name'] = file.name
            w, h = _image_dimensions(file)
            validated_data['width_px'] = w
            validated_data['height_px'] = h
        obj, _ = OrderItemDesign.objects.update_or_create(
            order_item=self.context['order_item'],
            print_area_name=validated_data['print_area_name'],
            defaults=validated_data,
        )
        return obj


class ColorCategoryListSerializer(serializers.ModelSerializer):
    class Meta:
        model = ColorCategoryList
        fields = ('id', 'category', 'colors', 'updated_at')
        read_only_fields = ('id', 'updated_at')


def _image_dimensions(file_obj):
    """Return (width_px, height_px) using Pillow. Gracefully returns (0, 0) on failure."""
    try:
        from PIL import Image
        file_obj.seek(0)
        with Image.open(file_obj) as img:
            return img.width, img.height
    except Exception:
        return 0, 0
