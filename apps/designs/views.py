from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated

from .models import SkuDesign
from .serializers import SkuDesignSerializer


def _sub_user(request):
    return request.user.sub_user_profile


# ── SKU Designs ───────────────────────────────────────────────────────────────

class SkuDesignListCreateView(generics.ListCreateAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = SkuDesignSerializer
    filterset_fields = ('sku', 'store', 'print_area_name', 'color_scope')

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
