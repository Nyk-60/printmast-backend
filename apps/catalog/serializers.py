from rest_framework import serializers

from .models import (
    Brand,
    Category,
    PrintArea,
    PrintCenterPrintArea,
    PrintCenterProduct,
    PrintCenterVariant,
    Product,
    ProductImage,
    ProductVariant,
)


# ── Platform catalog ──────────────────────────────────────────────────────────

class CategorySerializer(serializers.ModelSerializer):
    product_count = serializers.IntegerField(source='products.count', read_only=True)

    class Meta:
        model = Category
        fields = ('id', 'name', 'slug', 'parent', 'sort_order', 'product_count')


class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = ('id', 'name')


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ('id', 'image', 'sort_order')


class PrintAreaSerializer(serializers.ModelSerializer):
    class Meta:
        model = PrintArea
        fields = ('id', 'name', 'is_default', 'width_inches', 'height_inches', 'x', 'y', 'width_px', 'height_px')


class ProductVariantSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductVariant
        fields = ('id', 'sku', 'color', 'color_hex', 'size', 'dimension', 'volume',
                  'cost', 'price', 'is_dark', 'stock_status', 'weight_oz')


class ProductListSerializer(serializers.ModelSerializer):
    brand = serializers.CharField(source='brand.name')
    category = serializers.CharField(source='category.slug')

    class Meta:
        model = Product
        fields = ('id', 'name', 'sku', 'brand', 'category', 'base_price', 'badge', 'primary_image')


class ProductDetailSerializer(serializers.ModelSerializer):
    brand = BrandSerializer(read_only=True)
    category = CategorySerializer(read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    variants = ProductVariantSerializer(many=True, read_only=True)
    print_areas = PrintAreaSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = (
            'id', 'name', 'sku', 'description', 'brand', 'category',
            'base_cost', 'base_price', 'badge', 'is_active', 'primary_image',
            'images', 'variants', 'print_areas',
        )


class ProductWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = (
            'id', 'name', 'sku', 'description', 'brand', 'category',
            'base_cost', 'base_price', 'badge', 'is_active', 'primary_image',
        )


# ── Print center catalog ──────────────────────────────────────────────────────

class PrintCenterPrintAreaSerializer(serializers.ModelSerializer):
    class Meta:
        model = PrintCenterPrintArea
        fields = ('id', 'name', 'is_default', 'is_active', 'width_inches', 'height_inches')


class PrintCenterVariantSerializer(serializers.ModelSerializer):
    class Meta:
        model = PrintCenterVariant
        fields = ('id', 'color', 'color_hex', 'size', 'is_dark', 'is_active', 'cost', 'weight_oz')


class PrintCenterProductListSerializer(serializers.ModelSerializer):
    brand = serializers.CharField(source='brand.name', default=None)
    category = serializers.CharField(source='category.slug', default=None)
    variant_count = serializers.IntegerField(source='variants.count', read_only=True)

    class Meta:
        model = PrintCenterProduct
        fields = ('id', 'name', 'sku', 'brand', 'category', 'badge', 'is_active', 'primary_image', 'variant_count')


class PrintCenterProductDetailSerializer(serializers.ModelSerializer):
    variants = PrintCenterVariantSerializer(many=True, read_only=True)
    print_areas = PrintCenterPrintAreaSerializer(many=True, read_only=True)
    brand = BrandSerializer(read_only=True)
    category = CategorySerializer(read_only=True)
    source_product_id = serializers.IntegerField(source='source_product.id', read_only=True)

    class Meta:
        model = PrintCenterProduct
        fields = (
            'id', 'name', 'sku', 'description', 'brand', 'category',
            'badge', 'is_active', 'primary_image', 'source_product_id',
            'variants', 'print_areas',
        )


class PrintCenterProductWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = PrintCenterProduct
        fields = ('name', 'sku', 'description', 'brand', 'category', 'badge', 'is_active', 'primary_image')

    def validate_sku(self, value):
        print_center = self.context['print_center']
        qs = PrintCenterProduct.objects.filter(print_center=print_center, sku=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('SKU already exists in your catalog')
        return value

    def create(self, validated_data):
        return PrintCenterProduct.objects.create(
            print_center=self.context['print_center'],
            **validated_data,
        )


class AdoptProductSerializer(serializers.Serializer):
    """Adopt a platform product into the print center's catalog."""
    sku = serializers.CharField(max_length=64, required=False)
    variant_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False,
        help_text='Subset of variant IDs to include. Empty = all active variants.',
    )
    print_area_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False,
        help_text='Subset of print area IDs to include. Empty = all.',
    )
    cost_markup_percent = serializers.DecimalField(
        max_digits=6, decimal_places=2, default=0,
        help_text='Percentage markup over base_cost for initial variant pricing.',
    )
