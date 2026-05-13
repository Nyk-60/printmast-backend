from rest_framework import serializers

from .models import Brand, Category, PrintArea, Product, ProductImage, ProductVariant


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


class ProductVariantSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductVariant
        fields = (
            'id', 'sku', 'color', 'color_hex', 'size', 'dimension', 'volume',
            'cost', 'price', 'is_dark', 'stock_status'
        )


class PrintAreaSerializer(serializers.ModelSerializer):
    class Meta:
        model = PrintArea
        fields = ('id', 'name', 'extra_cost', 'is_default', 'x', 'y', 'width', 'height')


class ProductListSerializer(serializers.ModelSerializer):
    brand = serializers.CharField(source='brand.name')
    category = serializers.CharField(source='category.slug')

    class Meta:
        model = Product
        fields = (
            'id', 'name', 'sku', 'brand', 'category',
            'base_price', 'badge', 'primary_image'
        )


class ProductDetailSerializer(serializers.ModelSerializer):
    brand = BrandSerializer(read_only=True)
    category = CategorySerializer(read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    variants = ProductVariantSerializer(many=True, read_only=True)
    print_areas = PrintAreaSerializer(many=True, read_only=True)
    variation_types = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = (
            'id', 'name', 'sku', 'description', 'brand', 'category',
            'base_cost', 'base_price', 'badge', 'is_active', 'primary_image',
            'images', 'variants', 'print_areas', 'variation_types'
        )

    def get_variation_types(self, obj):
        return list(obj.variation_types.values_list('type', flat=True))
