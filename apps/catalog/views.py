from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from .models import Brand, Category, Product
from .serializers import (
    BrandSerializer,
    CategorySerializer,
    ProductDetailSerializer,
    ProductListSerializer,
)


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    permission_classes = (IsAuthenticated,)


class BrandViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer
    permission_classes = (IsAuthenticated,)


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Product.objects.filter(is_active=True).select_related('brand', 'category')
    permission_classes = (IsAuthenticated,)
    filterset_fields = ('category__slug', 'brand__name', 'badge')
    search_fields = ('name', 'sku', 'description')

    def get_serializer_class(self):
        if self.action == 'list':
            return ProductListSerializer
        return ProductDetailSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action != 'list':
            qs = qs.prefetch_related('images', 'variants', 'print_areas', 'variation_types')
        return qs
