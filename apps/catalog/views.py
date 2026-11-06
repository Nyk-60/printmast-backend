from decimal import Decimal
from io import BytesIO

from django.db import transaction

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsPrintCenter, IsSuperAdmin

from .models import (
    Brand,
    Category,
    PrintAreaSizeSpec,
    PrintCenterPrintArea,
    PrintCenterProduct,
    PrintCenterVariant,
    Product,
    ProductVariant,
    VariantDimensionRule,
    default_size_mode,
)
from .serializers import (
    AdoptProductSerializer,
    BrandSerializer,
    CategorySerializer,
    PrintAreaSizeSpecSerializer,
    PrintCenterPrintAreaSerializer,
    PrintCenterProductDetailSerializer,
    PrintCenterProductListSerializer,
    PrintCenterProductWriteSerializer,
    PrintCenterVariantSerializer,
    ProductDetailSerializer,
    ProductListSerializer,
    ProductVariantSerializer,
    ProductWriteSerializer,
    VariantDimensionRuleSerializer,
)


# ── Platform catalog (public read, superadmin write) ──────────────────────────

class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    permission_classes = (IsAuthenticated,)

    def _require_write_permission(self):
        user = self.request.user
        if not (user.is_superadmin or user.is_print_center):
            raise PermissionDenied('Only superadmin or print center users can modify categories')

    def create(self, request, *args, **kwargs):
        self._require_write_permission()
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        self._require_write_permission()
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        self._require_write_permission()
        return super().destroy(request, *args, **kwargs)


class BrandViewSet(viewsets.ModelViewSet):
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer
    permission_classes = (IsAuthenticated,)

    def _require_write_permission(self):
        user = self.request.user
        if not (user.is_superadmin or user.is_print_center):
            raise PermissionDenied('Only superadmin or print center users can modify brands')

    def _require_superadmin(self):
        if not self.request.user.is_superadmin:
            raise PermissionDenied('Only superadmin can perform this action')

    def create(self, request, *args, **kwargs):
        self._require_write_permission()
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        self._require_write_permission()
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        self._require_superadmin()
        return super().destroy(request, *args, **kwargs)


class ProductViewSet(viewsets.ModelViewSet):
    permission_classes = (IsAuthenticated,)
    filterset_fields = ('category__slug', 'brand__name', 'badge', 'is_active')
    search_fields = ('name', 'sku', 'description')

    def get_queryset(self):
        qs = Product.objects.select_related('brand', 'category')
        if self.action == 'list':
            return qs.filter(is_active=True).prefetch_related('variants')
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

        if sku and PrintCenterProduct.objects.filter(print_center=print_center, sku=sku).exists():
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
            variant1_label=product.variant1_label,
            variant2_label=product.variant2_label,
            age=product.age,
            subcategory=product.subcategory,
        )

        # Variants — new per-variant pricing format takes priority
        variant_overrides = data.get('variants') or []
        if variant_overrides:
            platform_variants = {v.id: v for v in product.variants.all()}
            for item in variant_overrides:
                vid = item.get('id')
                if vid:
                    pv = platform_variants.get(vid)
                    if not pv:
                        continue
                    PrintCenterVariant.objects.create(
                        pc_product=pc_product,
                        source_variant=pv,
                        color=pv.color,
                        color_hex=pv.color_hex,
                        color_group=pv.color_group,
                        size=pv.size,
                        is_dark=item.get('is_dark', pv.is_dark),
                        cost=Decimal(str(item['cost'])).quantize(Decimal('0.01')),
                        weight_oz=pv.weight_oz if item['weight_oz'] is None else item['weight_oz'],
                    )
                else:
                    PrintCenterVariant.objects.create(
                        pc_product=pc_product,
                        color=item.get('color', ''),
                        size=item.get('size', ''),
                        is_dark=item.get('is_dark', False),
                        cost=Decimal(str(item['cost'])).quantize(Decimal('0.01')),
                        weight_oz=item['weight_oz'] or Decimal('0'),
                    )
        else:
            # Legacy fallback: global markup with optional variant_ids filter
            markup = Decimal(str(data.get('cost_markup_percent') or 0)) / 100
            variant_ids = data.get('variant_ids') or []
            variants_qs = product.variants.all()
            if variant_ids:
                variants_qs = variants_qs.filter(id__in=variant_ids)
            for v in variants_qs:
                cost = v.cost * (1 + markup)
                PrintCenterVariant.objects.create(
                    pc_product=pc_product,
                    source_variant=v,
                    color=v.color,
                    color_hex=v.color_hex,
                    color_group=v.color_group,
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
        created_areas = []
        for area in areas_qs:
            created_areas.append(PrintCenterPrintArea.objects.create(
                pc_product=pc_product,
                source_print_area=area,
                name=area.name,
                is_default=area.is_default,
                width_inches=area.width_inches,
                height_inches=area.height_inches,
                size_mode=default_size_mode(area.name),
            ))
        for area in created_areas:
            area.link_to_counterpart()

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
            ctx['include_inactive'] = True  # PC management: show all variants
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

    def create(self, request, *args, **kwargs):
        pc_product = self.get_pc_product()
        data = request.data
        if isinstance(data, list):
            created = []
            for item in data:
                s = PrintCenterVariantSerializer(data=item)
                s.is_valid(raise_exception=True)
                created.append(s.save(pc_product=pc_product))
            return Response(PrintCenterVariantSerializer(created, many=True).data, status=status.HTTP_201_CREATED)
        s = PrintCenterVariantSerializer(data=data)
        s.is_valid(raise_exception=True)
        s.save(pc_product=pc_product)
        return Response(s.data, status=status.HTTP_201_CREATED)


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


class PrintAreaDimsView(APIView):
    """GET/POST size-specific max dimension specs for all print areas of a PC product."""
    permission_classes = (IsPrintCenter,)

    def _get_product(self, request, pk):
        try:
            return PrintCenterProduct.objects.get(pk=pk, print_center=request.user.print_center_profile)
        except PrintCenterProduct.DoesNotExist:
            raise NotFound()

    def get(self, request, pk):
        product = self._get_product(request, pk)
        specs = PrintAreaSizeSpec.objects.filter(
            print_area__pc_product=product
        ).select_related('print_area')
        return Response(PrintAreaSizeSpecSerializer(specs, many=True).data)

    def post(self, request, pk):
        product = self._get_product(request, pk)
        # areas that take their sizes from another area (right sleeve) have no grid of their own
        area_ids = set(product.print_areas.filter(size_from__isnull=True).values_list('id', flat=True))
        saved = 0
        for item in request.data:
            if int(item.get('print_area', 0)) not in area_ids:
                continue
            PrintAreaSizeSpec.objects.update_or_create(
                print_area_id=item['print_area'],
                size=item['size'],
                defaults={
                    'max_dim_in':    item.get('max_dim_in') or 0,
                    'max_width_in':  item.get('max_width_in') or 0,
                    'max_height_in': item.get('max_height_in') or 0,
                },
            )
            saved += 1
        return Response({'saved': saved})


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


class SubCatalogExportView(APIView):
    """Download all PC variants as Excel — for sub-users to use as mapping reference."""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        import io
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        from django.http import HttpResponse

        print_center = request.user.sub_user_profile.print_center
        products = (
            PrintCenterProduct.objects
            .filter(print_center=print_center, is_active=True)
            .prefetch_related('variants')
            .order_by('name')
        )

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'PC Catalog'

        header_fill = PatternFill(fill_type='solid', fgColor='DCFCE7')
        bold = Font(bold=True)

        headers = ['Product Name', 'PC Product SKU', 'Size', 'Color']
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = bold
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center')

        from apps.core.utils import size_order_annotation
        row = 2
        for product in products:
            for variant in (
                product.variants.filter(is_active=True)
                .annotate(size_ord=size_order_annotation())
                .order_by('size_ord', 'color')
            ):
                ws.cell(row=row, column=1, value=product.name)
                ws.cell(row=row, column=2, value=product.sku)
                ws.cell(row=row, column=3, value=variant.size)
                ws.cell(row=row, column=4, value=variant.color)
                row += 1

        for col, w in enumerate([40, 18, 10, 24], 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = w

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        resp = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        resp['Content-Disposition'] = 'attachment; filename="pc_catalog.xlsx"'
        return resp


class SubCatalogColorClassificationView(APIView):
    """Sub-user reads/writes per-product dark/light color classification."""
    permission_classes = (IsAuthenticated,)

    def _get_product(self, request, pk):
        try:
            return PrintCenterProduct.objects.prefetch_related('variants').get(
                pk=pk,
                print_center=request.user.sub_user_profile.print_center,
                is_active=True,
            )
        except PrintCenterProduct.DoesNotExist:
            raise NotFound()

    def get(self, request, pk):
        from apps.designs.models import SubUserProductColorSetting
        product  = self._get_product(request, pk)
        sub_user = request.user.sub_user_profile
        setting  = SubUserProductColorSetting.objects.filter(
            sub_user=sub_user, pc_product=product
        ).prefetch_related('entries').first()

        use_custom  = setting.use_custom if setting else False
        entries_map = {e.color_name: e.is_dark for e in setting.entries.all()} if setting else {}

        seen, colors = set(), []
        for v in product.variants.filter(is_active=True).order_by('color'):
            if v.color and v.color not in seen:
                seen.add(v.color)
                colors.append({
                    'name': v.color,
                    'pc_is_dark': v.is_dark,
                    'custom_is_dark': entries_map.get(v.color),
                })
        return Response({'use_custom': use_custom, 'colors': colors})

    def put(self, request, pk):
        from apps.designs.models import SubUserProductColorEntry, SubUserProductColorSetting
        product    = self._get_product(request, pk)
        sub_user   = request.user.sub_user_profile
        use_custom = bool(request.data.get('use_custom', False))
        entries_data = request.data.get('entries', [])

        setting, _ = SubUserProductColorSetting.objects.get_or_create(
            sub_user=sub_user, pc_product=product,
        )
        setting.use_custom = use_custom
        setting.save(update_fields=['use_custom'])

        setting.entries.all().delete()
        if use_custom and entries_data:
            SubUserProductColorEntry.objects.bulk_create([
                SubUserProductColorEntry(
                    setting=setting,
                    color_name=e['color_name'],
                    is_dark=bool(e['is_dark']),
                )
                for e in entries_data if e.get('color_name')
            ])

        return Response({'use_custom': use_custom, 'saved': True})


# ── Platform product variants (superadmin) ────────────────────────────────────

class PlatformVariantListCreateView(generics.ListCreateAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = ProductVariantSerializer

    def _get_product(self):
        try:
            return Product.objects.get(pk=self.kwargs['pk'])
        except Product.DoesNotExist:
            raise NotFound()

    def get_queryset(self):
        return self._get_product().variants.all()

    def create(self, request, *args, **kwargs):
        if not request.user.is_superadmin:
            raise PermissionDenied('Only superadmin can add variants')
        product = self._get_product()
        data = request.data
        if isinstance(data, list):
            created = []
            for item in data:
                s = ProductVariantSerializer(data=item)
                s.is_valid(raise_exception=True)
                created.append(s.save(product=product))
            return Response(ProductVariantSerializer(created, many=True).data, status=status.HTTP_201_CREATED)
        s = ProductVariantSerializer(data=data)
        s.is_valid(raise_exception=True)
        s.save(product=product)
        return Response(s.data, status=status.HTTP_201_CREATED)


class PlatformVariantDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = ProductVariantSerializer

    def _check_write(self):
        if not self.request.user.is_superadmin:
            raise PermissionDenied('Only superadmin can modify variants')

    def get_object(self):
        try:
            return ProductVariant.objects.get(
                pk=self.kwargs['variant_pk'],
                product__pk=self.kwargs['pk'],
            )
        except ProductVariant.DoesNotExist:
            raise NotFound()

    def update(self, request, *args, **kwargs):
        self._check_write()
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        self._check_write()
        return super().destroy(request, *args, **kwargs)


# ── Excel template & bulk upload (superadmin) ─────────────────────────────────

_TEMPLATE_HEADERS = [
    'Product Name', 'Product Code', 'Category', 'Brand', 'Description',
    'Price', 'Badge',
    'Size', 'Color', 'Weight (oz)', 'Is Dark', 'Color Group',
]

_TEMPLATE_EXAMPLES = [
    ['Gildan Unisex Sweatshirt', 'G18000', 'Sweatshirts', 'Gildan',
     'Heavy Blend 50/50 Crewneck', 12.00, 'popular', 'S', 'Black', 6.5, 'no', 'Dark'],
    ['Gildan Unisex Sweatshirt', 'G18000', '', '', '',
     12.00, '', 'M', 'Black', 6.8, 'no', 'Dark'],
    ['Gildan Unisex Sweatshirt', 'G18000', '', '', '',
     12.00, '', 'S', 'White', 6.5, 'no', 'Light'],
    ['Bella+Canvas Unisex Tee', 'BC3001', 'T-Shirts', 'Bella+Canvas',
     'Jersey Short Sleeve Tee', 8.50, 'bestseller', 'S', 'Black', 4.2, 'no', 'Dark'],
    ['Bella+Canvas Unisex Tee', 'BC3001', '', '', '',
     8.50, '', 'M', 'White', 4.5, 'no', 'Light'],
]

_TEMPLATE_INSTRUCTIONS = [
    ['Field', 'Required', 'Notes'],
    ['Product Name', 'YES', 'Rows with the same name = same product (different variants). Fill once, leave blank on repeat rows.'],
    ['Product Code', 'No', 'Internal code (e.g. G18000). Used to match duplicates on re-upload.'],
    ['Category', 'No', 'Auto-created if it does not exist. Fill on first row only.'],
    ['Brand', 'No', 'Auto-created if it does not exist. Fill on first row only.'],
    ['Description', 'No', 'Fill on first row only.'],
    ['Price', 'No', 'Selling price in USD (e.g. 12.00)'],
    ['Badge', 'No', 'One of: popular · new · bestseller'],
    ['Size', 'No', 'e.g. XS, S, M, L, XL, 2XL, 3XL'],
    ['Color', 'No', 'Color name, e.g. Black, White, Navy Blue'],
    ['Weight (oz)', 'No', 'Garment weight in ounces — required for shipping rate calculation'],
    ['Is Dark', 'No', 'yes or no — dark garments need a white underbase layer when printing'],
    ['Color Group', 'No', 'Color family for filtering, e.g. Light, Dark, Neutral, Bright'],
]

_COL_WIDTHS = [30, 14, 14, 16, 35, 11, 12, 7, 13, 12, 9, 14]


class ProductTemplateView(APIView):
    permission_classes = (IsSuperAdmin,)

    def get(self, request):
        from django.http import HttpResponse

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Products'

        accent = '2D9B84'
        hdr_fill = PatternFill(start_color=accent, end_color=accent, fill_type='solid')
        hdr_font = Font(bold=True, color='FFFFFF')

        for ci, h in enumerate(_TEMPLATE_HEADERS, 1):
            cell = ws.cell(row=1, column=ci, value=h)
            cell.fill = hdr_fill
            cell.font = hdr_font
            cell.alignment = Alignment(horizontal='center')

        for ri, row in enumerate(_TEMPLATE_EXAMPLES, 2):
            for ci, val in enumerate(row, 1):
                ws.cell(row=ri, column=ci, value=val)

        for ci, w in enumerate(_COL_WIDTHS, 1):
            ws.column_dimensions[get_column_letter(ci)].width = w

        ws2 = wb.create_sheet('Instructions')
        for row in _TEMPLATE_INSTRUCTIONS:
            ws2.append(row)
        for cell in ws2[1]:
            cell.font = Font(bold=True)
        ws2.column_dimensions['A'].width = 16
        ws2.column_dimensions['B'].width = 11
        ws2.column_dimensions['C'].width = 75

        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)

        response = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = 'attachment; filename="product_template.xlsx"'
        return response


class ProductUploadView(APIView):
    permission_classes = (IsSuperAdmin,)
    parser_classes = [MultiPartParser]

    def post(self, request):
        from decimal import Decimal, InvalidOperation

        file = request.FILES.get('file')
        if not file:
            return Response({'detail': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            wb = openpyxl.load_workbook(file, data_only=True)
            ws = wb.active
        except Exception:
            return Response({'detail': 'Invalid Excel file'}, status=status.HTTP_400_BAD_REQUEST)

        headers = [str(cell.value or '').strip() for cell in ws[1]]
        if 'Product Name' not in headers:
            return Response(
                {'detail': 'Missing required column: "Product Name"'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        def col(*keys):
            for k in keys:
                v = row_dict.get(k)
                if v is not None and str(v).strip():
                    return str(v).strip()
            return ''

        def to_dec(val, default='0'):
            try:
                return Decimal(str(val).replace(',', '.')) if val else Decimal(default)
            except InvalidOperation:
                return Decimal(default)

        created_products = 0
        created_variants = 0
        skipped_variants = 0
        errors = []

        with transaction.atomic():
            for row_num, raw_row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
                if all(v is None for v in raw_row):
                    continue

                row_dict = dict(zip(headers, raw_row))
                product_name = col('Product Name')
                if not product_name:
                    continue

                # Resolve category / brand (auto-create)
                category, brand = None, None
                cat_name = col('Category')
                if cat_name:
                    category, _ = Category.objects.get_or_create(name=cat_name)
                brand_name = col('Brand')
                if brand_name:
                    brand, _ = Brand.objects.get_or_create(name=brand_name)

                product_code = col('Product Code')
                price        = to_dec(col('Price', 'Base Price'))
                badge_raw    = col('Badge').lower()
                badge        = badge_raw if badge_raw in ('popular', 'new', 'bestseller') else ''

                # Find or create product (by code first, then by name)
                product = None
                if product_code:
                    product = Product.objects.filter(sku=product_code).first()
                if not product:
                    product = Product.objects.filter(name=product_name).first()
                if not product:
                    product = Product.objects.create(
                        name=product_name,
                        sku=product_code,
                        category=category,
                        brand=brand,
                        description=col('Description'),
                        base_price=price,
                        badge=badge,
                    )
                    created_products += 1

                # Create variant if size or color given
                size  = col('Size')
                color = col('Color')
                if size or color:
                    exists = ProductVariant.objects.filter(
                        product=product, size=size, color=color,
                    ).exists()
                    if exists:
                        skipped_variants += 1
                        continue
                    ProductVariant.objects.create(
                        product=product,
                        size=size,
                        color=color,
                        color_group=col('Color Group'),
                        price=price,
                        weight_oz=to_dec(col('Weight (oz)', 'Weight oz')),
                        is_dark=col('Is Dark').lower() in ('yes', 'evet', 'true', '1'),
                    )
                    created_variants += 1

        return Response({
            'created_products': created_products,
            'created_variants': created_variants,
            'skipped_variants': skipped_variants,
            'errors': errors,
        })


# ── Variant Dimension Rules (print center manages) ────────────────────────────

class VariantDimensionRuleListCreateView(APIView):
    permission_classes = (IsPrintCenter,)

    def _get_variant(self, request, pk, variant_pk):
        try:
            return PrintCenterVariant.objects.select_related('pc_product').get(
                pk=variant_pk,
                pc_product__pk=pk,
                pc_product__print_center=request.user.print_center_profile,
            )
        except PrintCenterVariant.DoesNotExist:
            raise NotFound()

    def get(self, request, pk, variant_pk):
        variant = self._get_variant(request, pk, variant_pk)
        return Response(VariantDimensionRuleSerializer(variant.dimension_rules.all(), many=True).data)

    def post(self, request, pk, variant_pk):
        variant = self._get_variant(request, pk, variant_pk)
        serializer = VariantDimensionRuleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule = serializer.save(variant=variant)
        return Response(VariantDimensionRuleSerializer(rule).data, status=status.HTTP_201_CREATED)


class VariantDimensionRuleDetailView(APIView):
    permission_classes = (IsPrintCenter,)

    def _get_rule(self, request, pk, variant_pk, rule_pk):
        try:
            return VariantDimensionRule.objects.select_related('variant__pc_product').get(
                pk=rule_pk,
                variant__pk=variant_pk,
                variant__pc_product__pk=pk,
                variant__pc_product__print_center=request.user.print_center_profile,
            )
        except VariantDimensionRule.DoesNotExist:
            raise NotFound()

    def patch(self, request, pk, variant_pk, rule_pk):
        rule = self._get_rule(request, pk, variant_pk, rule_pk)
        serializer = VariantDimensionRuleSerializer(rule, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk, variant_pk, rule_pk):
        rule = self._get_rule(request, pk, variant_pk, rule_pk)
        rule.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
