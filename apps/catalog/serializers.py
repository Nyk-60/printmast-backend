from django.db.models import Min
from rest_framework import serializers

from .models import (
    Brand,
    Category,
    PrintArea,
    PrintAreaSizeSpec,
    PrintCenterPrintArea,
    PrintCenterProduct,
    PrintCenterVariant,
    Product,
    ProductImage,
    ProductVariant,
    VariantDimensionRule,
    default_size_mode,
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
    sku   = serializers.CharField(max_length=80, required=False, allow_blank=True, default='')
    cost  = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=0)
    price = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=0)

    class Meta:
        model = ProductVariant
        fields = ('id', 'sku', 'color', 'color_hex', 'color_group', 'size', 'dimension', 'volume',
                  'cost', 'price', 'is_dark', 'stock_status', 'weight_oz')


class ProductListSerializer(serializers.ModelSerializer):
    brand          = serializers.CharField(source='brand.name')
    category       = serializers.CharField(source='category.slug', default=None)
    category_name  = serializers.CharField(source='category.name', default=None)
    variant_count = serializers.IntegerField(source='variants.count', read_only=True)
    size_count    = serializers.SerializerMethodField()
    color_count   = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ('id', 'name', 'sku', 'brand', 'category', 'category_name', 'base_price', 'badge',
                  'primary_image', 'variant_count', 'size_count', 'color_count',
                  'variant1_label', 'variant2_label', 'age', 'subcategory')

    def get_size_count(self, obj):
        return len({v.size for v in obj.variants.all() if v.size})

    def get_color_count(self, obj):
        return len({v.color for v in obj.variants.all() if v.color})


class ProductDetailSerializer(serializers.ModelSerializer):
    brand = BrandSerializer(read_only=True)
    category = CategorySerializer(read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    variants = serializers.SerializerMethodField()
    print_areas = PrintAreaSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = (
            'id', 'name', 'sku', 'description', 'brand', 'category',
            'base_price', 'badge', 'is_active', 'primary_image',
            'variant1_label', 'variant2_label', 'age', 'subcategory',
            'images', 'variants', 'print_areas',
        )

    def get_variants(self, obj):
        from apps.core.utils import size_order_annotation
        qs = obj.variants.annotate(size_ord=size_order_annotation()).order_by('size_ord', 'color')
        return ProductVariantSerializer(qs, many=True).data


class ProductWriteSerializer(serializers.ModelSerializer):
    sku           = serializers.CharField(max_length=64, required=False, allow_blank=True, default='')
    base_price    = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=0)
    primary_image = serializers.ImageField(required=False, allow_null=True, allow_empty_file=False)

    class Meta:
        model = Product
        fields = (
            'id', 'name', 'sku', 'description', 'brand', 'category',
            'base_price', 'badge', 'is_active', 'primary_image',
            'variant1_label', 'variant2_label', 'age', 'subcategory',
        )


# ── Print center catalog ──────────────────────────────────────────────────────

class PrintCenterPrintAreaSerializer(serializers.ModelSerializer):
    # Read-only, for display (e.g. the sub-user catalog's print-area summary) —
    # a follower (e.g. Right Sleeve) has no specs of its own, so this resolves
    # through size_from to the owner's, matching how sizing actually works.
    size_specs = serializers.SerializerMethodField()

    class Meta:
        model = PrintCenterPrintArea
        fields = ('id', 'name', 'is_default', 'is_active', 'width_inches', 'height_inches', 'icon',
                  'size_mode', 'size_from', 'size_specs')
        extra_kwargs = {'size_mode': {'required': False}}

    def get_size_specs(self, obj):
        owner = obj.size_from or obj
        return PrintAreaSizeSpecSerializer(owner.size_specs.all(), many=True).data

    def validate_size_from(self, target):
        if target is None:
            return target
        view = self.context.get('view')
        product_id = int(view.kwargs['pk']) if view is not None else None
        if product_id is not None and target.pc_product_id != product_id:
            raise serializers.ValidationError('Pick a print area of the same product.')
        if target.size_from_id:
            raise serializers.ValidationError('That area takes its sizes from another area.')
        if self.instance is not None:
            if target.pk == self.instance.pk:
                raise serializers.ValidationError('An area cannot take its sizes from itself.')
            if self.instance.size_followers.exists():
                raise serializers.ValidationError('Other areas take their sizes from this one.')
        return target

    def create(self, validated_data):
        if 'size_mode' not in validated_data:
            validated_data['size_mode'] = default_size_mode(validated_data.get('name'))
        area = super().create(validated_data)
        if area.size_from_id:
            area.size_mode = area.size_from.size_mode
            area.save(update_fields=['size_mode'])
        else:
            area.link_to_counterpart()
            area.refresh_from_db()
        return area

    def update(self, instance, validated_data):
        area = super().update(instance, validated_data)
        if area.size_from_id and area.size_mode != area.size_from.size_mode:
            area.size_mode = area.size_from.size_mode          # a follower always uses its owner's mode
            area.save(update_fields=['size_mode'])
        area.size_followers.exclude(size_mode=area.size_mode).update(size_mode=area.size_mode)
        return area


class PrintAreaSizeSpecSerializer(serializers.ModelSerializer):
    class Meta:
        model = PrintAreaSizeSpec
        fields = ('id', 'print_area', 'size', 'max_dim_in', 'max_width_in', 'max_height_in')


class VariantDimensionRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = VariantDimensionRule
        fields = ('id', 'qty_min', 'qty_max', 'length_in', 'width_in', 'height_in')


class PrintCenterVariantSerializer(serializers.ModelSerializer):
    dimension_rules = VariantDimensionRuleSerializer(many=True, read_only=True)

    class Meta:
        model = PrintCenterVariant
        fields = (
            'id', 'color', 'color_hex', 'color_group', 'size', 'is_dark', 'is_active', 'cost', 'weight_oz',
            'dim_length_in', 'dim_width_in', 'dim_height_base_in',
            'dim_height_per_unit_in', 'dim_height_step', 'is_stackable',
            'dimension_rules',
        )


class PrintCenterProductListSerializer(serializers.ModelSerializer):
    brand          = serializers.CharField(source='brand.name', default=None)
    category       = serializers.CharField(source='category.slug', default=None)
    category_name  = serializers.CharField(source='category.name', default=None)
    variant_count = serializers.IntegerField(source='variants.count', read_only=True)
    min_price     = serializers.SerializerMethodField()
    size_count    = serializers.SerializerMethodField()
    color_count   = serializers.SerializerMethodField()

    class Meta:
        model = PrintCenterProduct
        fields = ('id', 'name', 'sku', 'brand', 'category', 'category_name', 'badge', 'is_active',
                  'primary_image', 'variant_count', 'min_price', 'size_count', 'color_count',
                  'variant1_label', 'variant2_label', 'age', 'subcategory', 'sort_order')

    def get_min_price(self, obj):
        agg = obj.variants.filter(is_active=True).aggregate(m=Min('cost'))
        return agg['m']

    def get_size_count(self, obj):
        return len({v.size for v in obj.variants.all() if v.is_active and v.size})

    def get_color_count(self, obj):
        return len({v.color for v in obj.variants.all() if v.is_active and v.color})


class PrintCenterProductDetailSerializer(serializers.ModelSerializer):
    variants = serializers.SerializerMethodField()
    print_areas = PrintCenterPrintAreaSerializer(many=True, read_only=True)
    brand = BrandSerializer(read_only=True)
    category = CategorySerializer(read_only=True)
    source_product_id = serializers.IntegerField(source='source_product.id', read_only=True)

    class Meta:
        model = PrintCenterProduct
        fields = (
            'id', 'name', 'sku', 'description', 'brand', 'category',
            'badge', 'is_active', 'primary_image', 'source_product_id',
            'variant1_label', 'variant2_label', 'age', 'subcategory',
            'variants', 'print_areas',
        )

    def get_variants(self, obj):
        from apps.core.utils import size_order_annotation
        qs = obj.variants.all()
        if not self.context.get('include_inactive'):
            qs = qs.filter(is_active=True)
        qs = qs.annotate(size_ord=size_order_annotation()).order_by('size_ord', 'color')
        return PrintCenterVariantSerializer(qs, many=True).data


class PrintCenterProductWriteSerializer(serializers.ModelSerializer):
    primary_image = serializers.ImageField(required=False, allow_null=True, allow_empty_file=False)

    class Meta:
        model = PrintCenterProduct
        fields = ('name', 'sku', 'description', 'brand', 'category', 'badge',
                  'is_active', 'primary_image', 'variant1_label', 'variant2_label', 'age', 'subcategory', 'sort_order')
        extra_kwargs = {'sort_order': {'required': False}}

    def create(self, validated_data):
        pc = self.context['print_center']
        if 'sort_order' not in validated_data:
            last = PrintCenterProduct.objects.filter(print_center=pc).order_by('-sort_order').values_list('sort_order', flat=True).first()
            validated_data['sort_order'] = (last or 0) + 1000
        return PrintCenterProduct.objects.create(print_center=pc, **validated_data)


class AdoptVariantOverrideSerializer(serializers.Serializer):
    """Per-variant data when adopting a platform product."""
    id         = serializers.IntegerField(required=False)  # existing platform variant ID
    size       = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    color      = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    cost       = serializers.DecimalField(max_digits=10, decimal_places=2, default=0)
    weight_oz  = serializers.DecimalField(max_digits=8, decimal_places=2, required=False, allow_null=True, default=None)
    is_dark    = serializers.BooleanField(required=False, default=False)


class AdoptProductSerializer(serializers.Serializer):
    """Adopt a platform product into the print center's catalog."""
    sku            = serializers.CharField(max_length=64, required=False)
    variants       = AdoptVariantOverrideSerializer(many=True, required=False)
    print_area_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    # Legacy fields kept for backwards compatibility
    variant_ids        = serializers.ListField(child=serializers.IntegerField(), required=False)
    cost_markup_percent = serializers.DecimalField(max_digits=6, decimal_places=2, default=0, required=False)
