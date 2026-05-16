from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.orders.services import parse_color_category_excel

from .models import ColorCategoryList, SkuDesign
from .serializers import ColorCategoryListSerializer, SkuDesignSerializer


def _sub_user(request):
    return request.user.sub_user_profile


# ── SKU Designs ───────────────────────────────────────────────────────────────

class SkuDesignListCreateView(generics.ListCreateAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = SkuDesignSerializer
    filterset_fields = ('sku', 'print_area_name', 'color_scope')

    def get_queryset(self):
        return SkuDesign.objects.filter(sub_user=_sub_user(self.request)).order_by('-created_at')

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['sub_user'] = _sub_user(self.request)
        return ctx


class SkuDesignDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = SkuDesignSerializer

    def get_object(self):
        try:
            return SkuDesign.objects.get(pk=self.kwargs['pk'], sub_user=_sub_user(self.request))
        except SkuDesign.DoesNotExist:
            raise NotFound()

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['sub_user'] = _sub_user(self.request)
        return ctx


# ── Color Category Lists ──────────────────────────────────────────────────────

class ColorCategoryListView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        qs = ColorCategoryList.objects.filter(sub_user=_sub_user(request))
        return Response(ColorCategoryListSerializer(qs, many=True).data)

    def post(self, request):
        """Manually save dark/light color lists."""
        sub_user = _sub_user(request)
        serializer = ColorCategoryListSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        obj, _ = ColorCategoryList.objects.update_or_create(
            sub_user=sub_user,
            category=serializer.validated_data['category'],
            defaults={'colors': serializer.validated_data['colors']},
        )
        return Response(ColorCategoryListSerializer(obj).data, status=status.HTTP_201_CREATED)


class ColorCategoryUploadView(APIView):
    """Upload an Excel file with dark/light color columns."""
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        file = request.FILES.get('file')
        if not file:
            return Response({'detail': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
        dark_count, light_count = parse_color_category_excel(file, _sub_user(request))
        return Response({'dark_colors_saved': dark_count, 'light_colors_saved': light_count})
