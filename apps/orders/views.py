from django.db import transaction
from django.utils import timezone
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsPrintCenter
from apps.designs.models import OrderItemDesign
from apps.designs.serializers import OrderItemDesignSerializer

from .models import Order, OrderItem, ProductMapping
from .serializers import (
    AddressUpdateSerializer,
    MatchItemSerializer,
    OrderDetailSerializer,
    OrderListSerializer,
    ProductMappingSerializer,
)
from .services import (
    auto_match_design,
    auto_match_order,
    parse_mapping_excel,
)


def _get_sub_user(request):
    return getattr(request.user, 'sub_user_profile', None)


# ── Orders ────────────────────────────────────────────────────────────────────

class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAuthenticated,)
    filterset_fields = ('status', 'store')
    search_fields = ('shipstation_order_number', 'customer_name', 'external_order_number')

    def get_queryset(self):
        user = self.request.user
        if user.is_sub_user:
            return (
                Order.objects.filter(sub_user=user.sub_user_profile)
                .select_related('store')
                .prefetch_related('items__designs', 'items__pc_variant')
            )
        if user.is_print_center:
            return (
                Order.objects.filter(
                    store__shipstation_account__print_center=user.print_center_profile
                )
                .select_related('store', 'sub_user__user')
                .prefetch_related('items')
            )
        return Order.objects.none()

    def get_serializer_class(self):
        return OrderListSerializer if self.action == 'list' else OrderDetailSerializer

    # ── Address ──────────────────────────────────────────────────────────────

    @action(detail=True, methods=['patch'], url_path='address')
    def update_address(self, request, pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        serializer = AddressUpdateSerializer(order, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(OrderDetailSerializer(order).data)

    # ── Product match ─────────────────────────────────────────────────────────

    @action(detail=True, methods=['post'], url_path='match')
    def match(self, request, pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)

        data = request.data if isinstance(request.data, list) else [request.data]
        serializer = MatchItemSerializer(data=data, many=True)
        serializer.is_valid(raise_exception=True)

        for entry in serializer.validated_data:
            try:
                item = order.items.get(id=entry['item_id'])
            except OrderItem.DoesNotExist:
                continue
            item.pc_variant_id = entry['pc_variant_id']
            item.matched_at = timezone.now()
            item.save(update_fields=['pc_variant', 'matched_at'])

            if entry.get('remember'):
                ProductMapping.objects.update_or_create(
                    sub_user=request.user.sub_user_profile,
                    store=order.store,
                    marketplace_sku=item.marketplace_sku,
                    marketplace_variant=str(item.raw_options),
                    defaults={'pc_variant_id': entry['pc_variant_id']},
                )

            # Try to auto-match designs after product is matched
            if request.user.sub_user_profile.auto_design_matching:
                auto_match_design(item)

        self._refresh_order_status(order)
        return Response(OrderDetailSerializer(order).data)

    @action(detail=True, methods=['post'], url_path='auto-match')
    def run_auto_match(self, request, pk=None):
        """Trigger auto product + design matching for all unmatched items."""
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        matched = auto_match_order(order)
        return Response({'matched': matched})

    # ── Design upload ─────────────────────────────────────────────────────────

    @action(detail=True, methods=['post'], url_path='items/(?P<item_pk>[0-9]+)/design')
    def upload_design(self, request, pk=None, item_pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        try:
            item = order.items.get(pk=item_pk)
        except OrderItem.DoesNotExist:
            raise NotFound()

        serializer = OrderItemDesignSerializer(
            data=request.data,
            context={'order_item': item},
        )
        serializer.is_valid(raise_exception=True)
        design = serializer.save()
        return Response(OrderItemDesignSerializer(design).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='items/(?P<item_pk>[0-9]+)/designs')
    def list_designs(self, request, pk=None, item_pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        try:
            item = order.items.get(pk=item_pk)
        except OrderItem.DoesNotExist:
            raise NotFound()
        designs = OrderItemDesign.objects.filter(order_item=item)
        return Response(OrderItemDesignSerializer(designs, many=True).data)

    # ── Shipping ──────────────────────────────────────────────────────────────

    @action(detail=True, methods=['post'], url_path='select-shipping')
    def select_shipping(self, request, pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)

        carrier_code = request.data.get('carrier_code', '')
        service_code = request.data.get('service_code', '')
        displayed_rate = request.data.get('displayed_rate')

        if not carrier_code or not service_code:
            return Response({'detail': 'carrier_code and service_code required'}, status=400)

        order.selected_carrier_code = carrier_code
        order.selected_service_code = service_code
        if displayed_rate is not None:
            order.shipping_cost = displayed_rate
        order.save(update_fields=['selected_carrier_code', 'selected_service_code', 'shipping_cost'])
        return Response(OrderDetailSerializer(order).data)

    # ── Send to print ─────────────────────────────────────────────────────────

    @action(detail=True, methods=['post'], url_path='send-to-print')
    @transaction.atomic
    def send_to_print(self, request, pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)

        if order.status not in (Order.Status.READY, Order.Status.MATCHING):
            return Response({'detail': 'All items must be matched before sending to print'}, status=400)

        if not order.selected_carrier_code:
            return Response({'detail': 'Please select a shipping method first'}, status=400)

        sub_user = request.user.sub_user_profile
        total_cost = (order.shipping_cost or 0)

        # Check balance
        if not sub_user.can_afford(total_cost):
            return Response({'detail': 'Insufficient balance'}, status=402)

        # Debit balance
        from apps.accounts.models import BalanceTransaction
        sub_user_locked = type(sub_user).objects.select_for_update().get(pk=sub_user.pk)
        sub_user_locked.balance -= total_cost
        sub_user_locked.save(update_fields=['balance'])

        BalanceTransaction.objects.create(
            sub_user=sub_user_locked,
            type=BalanceTransaction.Type.DEBIT,
            amount=total_cost,
            shipping_amount=order.shipping_cost or 0,
            order_reference=order.shipstation_order_id,
            description=f'Order {order.shipstation_order_number} sent to print',
            created_by=request.user,
        )

        order.status = Order.Status.SENT_TO_PRINT
        order.save(update_fields=['status'])
        return Response(OrderDetailSerializer(order).data)

    # ── Cancel ────────────────────────────────────────────────────────────────

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        if order.status in (Order.Status.IN_PRODUCTION, Order.Status.SHIPPED):
            return Response({'detail': 'Cannot cancel order in this status'}, status=400)
        order.status = Order.Status.CANCELLED
        order.save(update_fields=['status'])
        return Response(OrderDetailSerializer(order).data)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _assert_sub_user_owns(self, request, order):
        if request.user.is_sub_user and order.sub_user != request.user.sub_user_profile:
            raise PermissionDenied()

    def _refresh_order_status(self, order):
        if not order.items.filter(pc_variant__isnull=True).exists():
            order.status = Order.Status.READY
            order.save(update_fields=['status'])


# ── Product Mappings ──────────────────────────────────────────────────────────

class ProductMappingListView(generics.ListAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = ProductMappingSerializer
    search_fields = ('marketplace_sku', 'marketplace_variant')

    def get_queryset(self):
        return ProductMapping.objects.filter(
            sub_user=_get_sub_user(self.request)
        ).select_related('pc_variant', 'store').order_by('-created_at')


class ProductMappingDeleteView(generics.DestroyAPIView):
    permission_classes = (IsAuthenticated,)

    def get_object(self):
        try:
            return ProductMapping.objects.get(
                pk=self.kwargs['pk'], sub_user=_get_sub_user(self.request)
            )
        except ProductMapping.DoesNotExist:
            raise NotFound()


class MappingUploadView(APIView):
    """Upload an Excel file to bulk-create/update ProductMappings."""
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        file = request.FILES.get('file')
        if not file:
            return Response({'detail': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)

        sub_user = _get_sub_user(request)
        store_id = request.data.get('store_id')
        store = None
        if store_id:
            from apps.shipping.models import Store
            try:
                store = Store.objects.get(pk=store_id, assigned_sub_users=sub_user)
            except Store.DoesNotExist:
                return Response({'detail': 'Store not found or not assigned to you'}, status=400)

        saved, skipped, errors = parse_mapping_excel(file, sub_user, store=store)
        return Response({'saved': saved, 'skipped': skipped, 'errors': errors[:20]})


# ── Print Center — production queue view ──────────────────────────────────────

class ProductionQueueView(generics.ListAPIView):
    """Orders sent to print, visible to the print center for processing."""
    permission_classes = (IsPrintCenter,)
    serializer_class = OrderListSerializer
    filterset_fields = ('sub_user', 'store')

    def get_queryset(self):
        return (
            Order.objects.filter(
                store__shipstation_account__print_center=self.request.user.print_center_profile,
                status=Order.Status.SENT_TO_PRINT,
            )
            .select_related('store', 'sub_user__user')
            .prefetch_related('items')
            .order_by('created_at')
        )
