from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated

from apps.accounts.scoping import get_sub_user_scope

from .models import SkuDesign
from .serializers import SkuDesignSerializer


def _sub_user(request):
    sub_user, _ = get_sub_user_scope(request.user)
    return sub_user


def _scope(request):
    return get_sub_user_scope(request.user)


# ── SKU Designs ───────────────────────────────────────────────────────────────

class SkuDesignListCreateView(generics.ListCreateAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = SkuDesignSerializer
    filterset_fields = ('sku', 'store', 'print_area_name', 'color_scope')

    def get_queryset(self):
        sub_user, store_ids = _scope(self.request)
        qs = SkuDesign.objects.filter(sub_user=sub_user)
        if store_ids is not None:
            qs = qs.filter(store_id__in=store_ids)
        return qs.order_by('-created_at')

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['sub_user'] = _sub_user(self.request)
        return ctx


class SkuDesignDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = SkuDesignSerializer

    def get_object(self):
        sub_user, store_ids = _scope(self.request)
        try:
            qs = SkuDesign.objects.filter(sub_user=sub_user)
            if store_ids is not None:
                qs = qs.filter(store_id__in=store_ids)
            return qs.get(pk=self.kwargs['pk'])
        except SkuDesign.DoesNotExist:
            raise NotFound()

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['sub_user'] = _sub_user(self.request)
        return ctx
