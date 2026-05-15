from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Order, OrderItem, ProductMapping
from .serializers import (
    AddressUpdateSerializer,
    MatchItemSerializer,
    OrderDetailSerializer,
    OrderListSerializer,
)


class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAuthenticated,)
    filterset_fields = ('status', 'store')
    search_fields = ('shipstation_order_number', 'customer_name', 'external_order_number')

    def get_queryset(self):
        user = self.request.user
        if user.is_sub_user:
            return Order.objects.filter(
                sub_user=user.sub_user_profile
            ).select_related('store').prefetch_related('items')
        if user.is_print_center:
            return Order.objects.filter(
                store__shipstation_account__print_center=user.print_center_profile
            ).select_related('store', 'sub_user__user').prefetch_related('items')
        return Order.objects.none()

    def get_serializer_class(self):
        if self.action == 'list':
            return OrderListSerializer
        return OrderDetailSerializer

    @action(detail=True, methods=['patch'], url_path='address')
    def update_address(self, request, pk=None):
        order = self.get_object()
        serializer = AddressUpdateSerializer(order, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(OrderDetailSerializer(order).data)

    @action(detail=True, methods=['post'], url_path='match')
    def match(self, request, pk=None):
        order = self.get_object()
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

        if not order.items.filter(pc_variant__isnull=True).exists():
            order.status = Order.Status.READY
            order.save(update_fields=['status'])

        return Response(OrderDetailSerializer(order).data)

    @action(detail=True, methods=['post'], url_path='send-to-print')
    def send_to_print(self, request, pk=None):
        order = self.get_object()
        if order.status not in (Order.Status.READY, Order.Status.MATCHING):
            return Response(
                {'detail': 'Order is not ready for production'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        order.status = Order.Status.SENT_TO_PRINT
        order.save(update_fields=['status'])
        # Balance debit + label purchase handled in production phase
        return Response(OrderDetailSerializer(order).data)
