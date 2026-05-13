from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Order, OrderItem, UserProductMapping
from .serializers import (
    AddressUpdateSerializer,
    MatchItemSerializer,
    OrderDetailSerializer,
    OrderListSerializer,
)


class OrderViewSet(viewsets.ModelViewSet):
    permission_classes = (IsAuthenticated,)
    filterset_fields = ('status', 'store')
    search_fields = ('external_order_number', 'customer_name')

    def get_queryset(self):
        return Order.objects.filter(owner=self.request.user).select_related('store').prefetch_related('items')

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
        # TODO: push update to ShipStation via apps.shipping.services
        return Response(OrderDetailSerializer(order).data)

    @action(detail=True, methods=['post'], url_path='match')
    def match(self, request, pk=None):
        order = self.get_object()
        serializer = MatchItemSerializer(data=request.data, many=isinstance(request.data, list))
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data if isinstance(serializer.validated_data, list) else [serializer.validated_data]

        for entry in payload:
            try:
                item = order.items.get(id=entry['item_id'])
            except OrderItem.DoesNotExist:
                continue
            item.catalog_variant_id = entry['catalog_variant_id']
            item.matched_at = timezone.now()
            item.save(update_fields=['catalog_variant', 'matched_at'])

            if entry.get('remember') and item.raw_variant:
                UserProductMapping.objects.update_or_create(
                    owner=request.user,
                    etsy_variant_name=item.raw_variant,
                    defaults={'catalog_variant_id': entry['catalog_variant_id']},
                )

        if not order.items.filter(catalog_variant__isnull=True).exists():
            order.status = 'matched'
            order.save(update_fields=['status'])

        return Response(OrderDetailSerializer(order).data)

    @action(detail=True, methods=['post'], url_path='send-to-production')
    def send_to_production(self, request, pk=None):
        order = self.get_object()
        if order.status != 'matched':
            return Response({'detail': 'Order must be matched first'}, status=status.HTTP_400_BAD_REQUEST)
        order.status = 'in_production'
        order.save(update_fields=['status'])
        # TODO: create label via ShipStation, debit balance, enqueue production job
        return Response(OrderDetailSerializer(order).data)
