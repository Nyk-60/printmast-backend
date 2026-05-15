from decimal import Decimal

from django.db import transaction
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsPrintCenter, IsSuperAdmin

from .models import (
    Brand,
    Category,
    PrintCenterPrintArea,
    PrintCenterProduct,
    PrintCenterVariant,
    Product,
    ProductVariant,
)
from .serializers import (
    AdoptProductSerializer,
    BrandSerializer,
    CategorySerializer,
    PrintCenterPrintAreaSerializer,
    PrintCenterProductDetailSerializer,
    PrintCenterProductListSerializer,
    PrintCenterProductWriteSerializer,
    PrintCenterVariantSerializer,
    ProductDetailSerializer,
    ProductListSerializer,
    ProductWriteSerializer,
)


# ── Platform catalog (public read, superadmin write) ──────────────────────────

class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    permission_classes = (IsAuthenticated,)


class BrandViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer
    permission_classes = (IsAuthenticated,)


class ProductViewSet(viewsets.ModelViewSet):
    permission_classes = (IsAuthenticated,)
    filterset_fields = ('category__slug', 'brand__name', 'badge', 'is_active')
    search_fields = ('name', 'sku', 'description')

    def get_queryset(self):
        qs = Product.objects.select_related('brand', 'category')
        if self.action == 'list':
            return qs.filter(is_active=True)
        return qs.prefetch_related('images', 'variants', 'print_areas')

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return ProductWriteSerializer
        if self.action == 'list':
            return ProductListSerializer
        return ProductDetailSerializer

    def check_write_permission(self):
        if not self.request.user.is_superadmin:
            raise PermissionDenied('Only superadmin can modify platform products')

    def create(self, request, *args, **kwargs):
        self.check_write_permission()
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        self.check_write_permission()
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        self.check_write_permission()
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'], permission_classes=[IsPrintCenter])
    @transaction.atomic
    def adopt(self, request, pk=None):
        """Adopt this platform product into the print center's catalog."""
        product = self.get_object()
        serializer = AdoptProductSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        print_center = request.user.print_center_profile
        sku = data.get('sku') or product.sku

        if PrintCenterProduct.objects.filter(print_center=print_center, sku=sku).exists():
            return Response({'detail': 'A product with this SKU already exists in your catalog'}, status=400)

        pc_product = PrintCenterProduct.objects.create(
            print_center=print_center,
            source_product=product,
            name=product.name,
            sku=sku,
            description=product.description,
            category=product.category,
            brand=product.brand,
            badge=product.badge,
            primary_image=product.primary_image,
        )

        markup = Decimal(str(data.get('cost_markup_percent') or 0)) / 100

        # Variants
        variant_ids = data.get('variant_ids') or []
        variants_qs = product.variants.filter(is_active=True) if hasattr(product.variants.model, 'is_active') else product.variants.all()
        if variant_ids:
            variants_qs = variants_qs.filter(id__in=variant_ids)
        for v in variants_qs:
            cost = v.cost * (1 + markup)
            PrintCenterVariant.objects.create(
                pc_product=pc_product,
                source_variant=v,
                color=v.color,
                color_hex=v.color_hex,
                size=v.size,
                is_dark=v.is_dark,
                cost=cost.quantize(Decimal('0.01')),
                weight_oz=v.weight_oz,
            )

        # Print areas
        area_ids = data.get('print_area_ids') or []
        areas_qs = product.print_areas.all()
        if area_ids:
            areas_qs = areas_qs.filter(id__in=area_ids)
        for area in areas_qs:
            PrintCenterPrintArea.objects.create(
                pc_product=pc_product,
                source_print_area=area,
                name=area.name,
                is_default=area.is_default,
                width_inches=area.width_inches,
                height_inches=area.height_inches,
            )

        return Response(PrintCenterProductDetailSerializer(pc_product).data, status=status.HTTP_201_CREATED)


# ── Print center catalog ──────────────────────────────────────────────────────

class PrintCenterCatalogView(generics.ListAPIView):
    """List the calling print center's catalog products."""
    permission_classes = (IsPrintCenter,)
    serializer_class = PrintCenterProductListSerializer
    search_fields = ('name', 'sku')
    filterset_fields = ('is_active', 'badge')

    def get_queryset(self):
        return PrintCenterProduct.objects.filter(
            print_center=self.request.user.print_center_profile
        ).select_related('brand', 'category').prefetch_related('variants')


class PrintCenterProductView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = (IsPrintCenter,)

    def get_object(self):
        try:
            return PrintCenterProduct.objects.prefetch_related('variants', 'print_areas').get(
                pk=self.kwargs['pk'],
                print_center=self.request.user.print_center_profile,
            )
        except PrintCenterProduct.DoesNotExist:
            raise NotFound()

    def get_serializer_class(self):
        if self.request.method in ('PUT', 'PATCH'):
            return PrintCenterProductWriteSerializer
        return PrintCenterProductDetailSerializer

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self.request.user.is_print_center:
            ctx['print_center'] = self.request.user.print_center_profile
        return ctx


class PrintCenterProductCreateView(generics.CreateAPIView):
    permission_classes = (IsPrintCenter,)
    serializer_class = PrintCenterProductWriteSerializer

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['print_center'] = self.request.user.print_center_profile
        return ctx

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pc_product = serializer.save()
        return Response(PrintCenterProductDetailSerializer(pc_product).data, status=status.HTTP_201_CREATED)


class PrintCenterVariantView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = (IsPrintCenter,)
    serializer_class = PrintCenterVariantSerializer

    def get_object(self):
        try:
            return PrintCenterVariant.objects.select_related('pc_product__print_center').get(
                pk=self.kwargs['variant_pk'],
                pc_product__pk=self.kwargs['pk'],
                pc_product__print_center=self.request.user.print_center_profile,
            )
        except PrintCenterVariant.DoesNotExist:
            raise NotFound()


class PrintCenterVariantCreateView(generics.CreateAPIView):
    permission_classes = (IsPrintCenter,)
    serializer_class = PrintCenterVariantSerializer

    def get_pc_product(self):
        try:
            return PrintCenterProduct.objects.get(
                pk=self.kwargs['pk'],
                print_center=self.request.user.print_center_profile,
            )
        except PrintCenterProduct.DoesNotExist:
            raise NotFound()

    def perform_create(self, serializer):
        serializer.save(pc_product=self.get_pc_product())


class PrintCenterPrintAreaView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = (IsPrintCenter,)
    serializer_class = PrintCenterPrintAreaSerializer

    def get_object(self):
        try:
            return PrintCenterPrintArea.objects.select_related('pc_product__print_center').get(
                pk=self.kwargs['area_pk'],
                pc_product__pk=self.kwargs['pk'],
                pc_product__print_center=self.request.user.print_center_profile,
            )
        except PrintCenterPrintArea.DoesNotExist:
            raise NotFound()


class PrintCenterPrintAreaCreateView(generics.CreateAPIView):
    permission_classes = (IsPrintCenter,)
    serializer_class = PrintCenterPrintAreaSerializer

    def get_pc_product(self):
        try:
            return PrintCenterProduct.objects.get(
                pk=self.kwargs['pk'],
                print_center=self.request.user.print_center_profile,
            )
        except PrintCenterProduct.DoesNotExist:
            raise NotFound()

    def perform_create(self, serializer):
        serializer.save(pc_product=self.get_pc_product())


# ── Sub-user view (read-only, their print center's active catalog) ─────────────

class MyCatalogView(generics.ListAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = PrintCenterProductListSerializer
    search_fields = ('name', 'sku')

    def get_queryset(self):
        return PrintCenterProduct.objects.filter(
            print_center=self.request.user.sub_user_profile.print_center,
            is_active=True,
        ).select_related('brand', 'category').prefetch_related('variants')


class MyCatalogProductView(generics.RetrieveAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = PrintCenterProductDetailSerializer

    def get_object(self):
        try:
            return PrintCenterProduct.objects.prefetch_related('variants', 'print_areas').get(
                pk=self.kwargs['pk'],
                print_center=self.request.user.sub_user_profile.print_center,
                is_active=True,
            )
        except PrintCenterProduct.DoesNotExist:
            raise NotFound()
