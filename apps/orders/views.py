from django.db import transaction
from django.utils import timezone
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsPrintCenter
from apps.designs.models import OrderItemDesign, SkuDesign
from apps.designs.serializers import OrderItemDesignSerializer

from .models import Order, OrderEvent, OrderItem, ProductMapping
from .serializers import (
    AddressUpdateSerializer,
    MatchItemSerializer,
    OrderActivityLogSerializer,
    OrderDetailSerializer,
    OrderListSerializer,
    ProductMappingSerializer,
    ShippingOverrideSerializer,
)
from .services import (
    auto_match_design,
    auto_match_order,
    log_event,
    order_is_matched,
    parse_mapping_excel,
    refresh_order_status,
)


def _get_sub_user(request):
    return getattr(request.user, 'sub_user_profile', None)


# ── Orders ────────────────────────────────────────────────────────────────────

class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAuthenticated,)
    filterset_fields = ('status', 'store', 'sub_user')
    search_fields = (
        'shipstation_order_number', 'customer_name', 'external_order_number', 'ship_to_name',
        'tracking_number',
        'items__marketplace_sku', 'items__raw_product_name',
        'items__pc_variant__pc_product__name', 'items__pc_variant__pc_product__brand__name',
        'items__pc_variant__size', 'items__pc_variant__color',
    )

    NEW_ORDER_STATUSES       = ('new', 'matching', 'ready')
    READY_FOR_PRINT_STATUSES = ('sent_to_print',)
    IN_PRODUCTION_STATUSES   = ('in_production',)
    ACTIVE_STATUSES          = ('new', 'matching', 'ready', 'sent_to_print', 'in_production')
    COMPLETED_STATUSES       = ('shipped', 'delivered', 'cancelled', 'refunded')

    def get_queryset(self):
        from django.db.models import Count, Exists, OuterRef, Q
        from apps.designs.models import OrderItemDesign

        user   = self.request.user
        params = self.request.query_params
        tab    = params.get('tab', '')

        if user.is_sub_user:
            qs = (
                Order.objects.filter(sub_user=user.sub_user_profile, is_hidden=False)
                .select_related('store__shipstation_account__print_center', 'sub_user__user')
                .prefetch_related('items__pc_variant__pc_product')
            )
        elif user.is_print_center:
            qs = (
                Order.objects.filter(
                    store__shipstation_account__print_center=user.print_center_profile,
                    is_hidden=False,
                )
                .select_related('store', 'sub_user__user')
                .prefetch_related('items__pc_variant__pc_product')
            )
        else:
            return Order.objects.none()

        # Tab filtering (new_orders / ready_for_print / in_production / completed; 'active' = backward compat)
        if tab in ('new_orders', 'active'):
            qs = qs.filter(status__in=self.NEW_ORDER_STATUSES)
        elif tab == 'ready_for_print':
            qs = qs.filter(status__in=self.READY_FOR_PRINT_STATUSES)
        elif tab == 'in_production':
            qs = qs.filter(status__in=self.IN_PRODUCTION_STATUSES)
        elif tab == 'completed':
            qs = qs.filter(status__in=self.COMPLETED_STATUSES)

        # Always annotate has_design for serializer + optional filter
        qs = qs.annotate(
            _has_design=Exists(
                OrderItemDesign.objects.filter(order_item__order=OuterRef('pk'))
            )
        )

        # matched=true → all billable items matched; matched=false → has unmatched
        matched_p = params.get('matched', '')
        if matched_p in ('true', 'false'):
            qs = qs.annotate(
                _unmatched=Count(
                    'items',
                    filter=Q(items__pc_variant__isnull=True, items__unit_price__gte=0),
                )
            )
            if matched_p == 'true':
                qs = qs.filter(_unmatched=0)
            else:
                qs = qs.filter(_unmatched__gt=0)

        # has_design=true/false filter
        has_design_p = params.get('has_design', '')
        if has_design_p == 'true':
            qs = qs.filter(_has_design=True)
        elif has_design_p == 'false':
            qs = qs.filter(_has_design=False)

        return qs

    def get_serializer_class(self):
        return OrderListSerializer if self.action == 'list' else OrderDetailSerializer

    # ── Tab counts ───────────────────────────────────────────────────────────

    @action(detail=False, methods=['get'], url_path='tab-counts')
    def tab_counts(self, request):
        """Order count per status tab, honoring the same store/sub_user/search
        filters as the list endpoint (but ignoring `tab` itself)."""
        from django.db.models import Count

        user = request.user
        if user.is_sub_user:
            base_qs = Order.objects.filter(sub_user=user.sub_user_profile, is_hidden=False)
        elif user.is_print_center:
            base_qs = Order.objects.filter(
                store__shipstation_account__print_center=user.print_center_profile,
                is_hidden=False,
            )
        else:
            base_qs = Order.objects.none()

        qs = self.filter_queryset(base_qs)
        counts = {row['status']: row['c'] for row in qs.values('status').annotate(c=Count('id'))}

        def total(statuses):
            return sum(counts.get(s, 0) for s in statuses)

        return Response({
            'new_orders':      total(self.NEW_ORDER_STATUSES),
            'ready_for_print': total(self.READY_FOR_PRINT_STATUSES),
            'in_production':   total(self.IN_PRODUCTION_STATUSES),
            'completed':       total(self.COMPLETED_STATUSES),
        })

    @action(detail=False, methods=['get'], url_path='ready-for-print-summary')
    def ready_for_print_summary(self, request):
        """Live running total for everything currently queued in Ready for Print,
        honoring the same store/search filters as the list endpoint."""
        from decimal import Decimal

        from .services import calculate_order_cost

        user = request.user
        if user.is_sub_user:
            base_qs = Order.objects.filter(sub_user=user.sub_user_profile, is_hidden=False)
        elif user.is_print_center:
            base_qs = Order.objects.filter(
                store__shipstation_account__print_center=user.print_center_profile,
                is_hidden=False,
            )
        else:
            base_qs = Order.objects.none()

        qs = self.filter_queryset(
            base_qs.filter(status__in=self.READY_FOR_PRINT_STATUSES)
        ).prefetch_related('items__pc_variant', 'items__designs')

        count, total = 0, Decimal('0')
        for order in qs:
            count += 1
            _, _, _, order_total = calculate_order_cost(order)
            total += order_total

        return Response({'count': count, 'total': total})

    # ── Activity log (PC: search order history by order no / customer / tracking) ──

    @action(detail=False, methods=['get'], url_path='activity-log', permission_classes=[IsPrintCenter])
    def activity_log(self, request):
        search = (request.query_params.get('search') or '').strip()
        if not search:
            return Response([])

        base_qs = Order.objects.filter(
            store__shipstation_account__print_center=request.user.print_center_profile,
        ).select_related('store', 'sub_user__user').prefetch_related('events__actor')

        qs = self.filter_queryset(base_qs).order_by('-created_at')[:25]
        return Response(OrderActivityLogSerializer(qs, many=True).data)

    # ── Address ──────────────────────────────────────────────────────────────

    @action(detail=True, methods=['patch'], url_path='address')
    def update_address(self, request, pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        serializer = AddressUpdateSerializer(order, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # Push updated address to ShipStation in a background thread (non-blocking)
        import threading
        def _push():
            try:
                from django.db import close_old_connections
                close_old_connections()
                from apps.shipping.tasks import push_address_to_shipstation
                push_address_to_shipstation(order.pk)
            except Exception:
                pass
        threading.Thread(target=_push, daemon=True).start()

        return Response(OrderDetailSerializer(order).data)

    @action(detail=True, methods=['patch'], url_path='pc-address', permission_classes=[IsPrintCenter])
    def pc_update_address(self, request, pk=None):
        """Print center fixes a delivery address (e.g. one a label purchase rejected).
        Only before the label exists — once bought, the label carries the old address."""
        order = self.get_object()
        if order.labeled_at or order.status != Order.Status.SENT_TO_PRINT:
            return Response(
                {'detail': 'The address can only be changed while the order is waiting to be labeled.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = AddressUpdateSerializer(order, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        order.address_status = Order.AddressStatus.UNKNOWN
        order.address_verified = False
        order.save(update_fields=['address_status', 'address_verified'])
        log_event(order, OrderEvent.EventType.ADDRESS_EDITED, actor=request.user,
                  description='Delivery address edited by the print center')

        import threading

        def _push():
            try:
                from django.db import close_old_connections
                close_old_connections()
                from apps.shipping.tasks import push_address_to_shipstation
                push_address_to_shipstation(order.pk)
            except Exception:
                pass
        threading.Thread(target=_push, daemon=True).start()

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
            log_event(
                order, OrderEvent.EventType.MATCHED, actor=request.user,
                description=f'{item.raw_product_name} → {item.pc_variant}',
            )

            if entry.get('remember'):
                from .services import _variant_key
                ProductMapping.objects.update_or_create(
                    sub_user=request.user.sub_user_profile,
                    store=order.store,
                    marketplace_sku=item.marketplace_sku,
                    marketplace_variant=_variant_key(item.raw_options or {}),
                    defaults={'pc_variant_id': entry['pc_variant_id']},
                )

            # Try to auto-match designs after product is matched
            if request.user.sub_user_profile.auto_design_matching:
                auto_match_design(item, actor=request.user)

        self._refresh_order_status(order, actor=request.user)
        return Response(OrderDetailSerializer(order).data)

    @action(detail=True, methods=['post'], url_path='auto-match')
    def run_auto_match(self, request, pk=None):
        """Trigger auto product + design matching for all unmatched items."""
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        matched = auto_match_order(order, actor=request.user)
        self._refresh_order_status(order, actor=request.user)
        return Response({'matched': matched})

    # ── Design upload ─────────────────────────────────────────────────────────

    @action(detail=True, methods=['patch'], url_path='items/(?P<item_pk>[0-9]+)/personalization')
    def update_personalization(self, request, pk=None, item_pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        try:
            item = order.items.get(pk=item_pk)
        except OrderItem.DoesNotExist:
            raise NotFound()
        item.personalization = request.data.get('personalization', '')
        item.save(update_fields=['personalization'])

        import threading
        def _push():
            try:
                from django.db import close_old_connections
                close_old_connections()
                from apps.shipping.tasks import push_personalization_to_shipstation
                push_personalization_to_shipstation(order.pk, item.pk)
            except Exception:
                pass
        threading.Thread(target=_push, daemon=True).start()

        return Response({'personalization': item.personalization})

    @action(detail=True, methods=['post'], url_path='items/(?P<item_pk>[0-9]+)/image',
            parser_classes=[__import__('rest_framework.parsers', fromlist=['MultiPartParser']).MultiPartParser])
    def upload_item_image(self, request, pk=None, item_pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        try:
            item = order.items.get(pk=item_pk)
        except OrderItem.DoesNotExist:
            raise NotFound()
        image = request.FILES.get('image')
        if not image:
            return Response({'detail': 'No image provided'}, status=400)
        if item.custom_image:
            item.custom_image.delete(save=False)
        item.custom_image = image
        item.save(update_fields=['custom_image'])
        from .serializers import OrderItemPreviewSerializer
        return Response(OrderItemPreviewSerializer(item, context={'request': request}).data)

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
            context={'order_item': item, 'sub_user': request.user.sub_user_profile, 'request': request},
        )
        serializer.is_valid(raise_exception=True)
        design = serializer.save()
        log_event(
            order, OrderEvent.EventType.DESIGN_UPLOADED, actor=request.user,
            description=f'{item.raw_product_name} — {design.print_area_name}: {design.file_name}',
        )
        self._refresh_order_status(order, actor=request.user)
        resp = OrderItemDesignSerializer(design, context={'request': request}).data
        # When the upload scope doesn't match the item's color (e.g., dark on a white shirt),
        # the OID isn't updated so its sku_design still points to the previous matched design.
        # Override response fields with the SkuDesign that was actually saved so the frontend
        # can add a correct separate card for it.
        saved_sd = serializer.context.get('saved_sku_design')
        if saved_sd:
            resp['sku_design'] = saved_sd.pk
            resp['color_scope'] = saved_sd.color_scope
            resp['specific_color'] = saved_sd.specific_color
            resp['file_name'] = saved_sd.file_name
            if saved_sd.file:
                resp['file'] = request.build_absolute_uri(saved_sd.file.url)
        return Response(resp, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='items/(?P<item_pk>[0-9]+)/designs')
    def list_designs(self, request, pk=None, item_pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        try:
            item = order.items.get(pk=item_pk)
        except OrderItem.DoesNotExist:
            raise NotFound()
        designs = OrderItemDesign.objects.filter(order_item=item)
        return Response(OrderItemDesignSerializer(designs, many=True, context={'request': request}).data)

    @action(detail=True, methods=['delete'], url_path='items/(?P<item_pk>[0-9]+)/design/(?P<design_pk>[0-9]+)')
    def delete_design(self, request, pk=None, item_pk=None, design_pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        try:
            item = order.items.get(pk=item_pk)
        except OrderItem.DoesNotExist:
            raise NotFound()
        try:
            design = OrderItemDesign.objects.get(pk=design_pk, order_item=item)
        except OrderItemDesign.DoesNotExist:
            raise NotFound()

        log_event(
            order, OrderEvent.EventType.DESIGN_REMOVED, actor=request.user,
            description=f'{item.raw_product_name} — {design.print_area_name}',
        )
        if design.file:
            design.file.delete(save=False)
        design.delete()
        self._refresh_order_status(order, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['delete'], url_path='items/(?P<item_pk>[0-9]+)/library-design/(?P<sku_design_pk>[0-9]+)')
    def delete_library_design(self, request, pk=None, item_pk=None, sku_design_pk=None):
        """Delete a SkuDesign (library entry) and its associated OrderItemDesigns in this order."""
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        sub_user = request.user.sub_user_profile
        try:
            sku_design = SkuDesign.objects.get(pk=sku_design_pk, sub_user=sub_user)
        except SkuDesign.DoesNotExist:
            raise NotFound()

        # Remove any OrderItemDesign in this order that was linked to this SkuDesign
        OrderItemDesign.objects.filter(sku_design=sku_design, order_item__order=order).delete()
        log_event(
            order, OrderEvent.EventType.DESIGN_REMOVED, actor=request.user,
            description=f'Library design removed: {sku_design.file_name}',
        )

        if sku_design.file:
            sku_design.file.delete(save=False)
        sku_design.delete()

        self._refresh_order_status(order, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

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
            # Must be Decimal, not a raw JSON float — calculate_order_cost() adds
            # this to other Decimal values in the very same request/response cycle
            # (OrderDetailSerializer below), and Decimal + float raises TypeError.
            from decimal import Decimal
            order.shipping_cost = Decimal(str(displayed_rate))
        order.save(update_fields=['selected_carrier_code', 'selected_service_code', 'shipping_cost'])
        return Response(OrderDetailSerializer(order).data)

    # ── Send to print ─────────────────────────────────────────────────────────

    @action(detail=True, methods=['post'], url_path='send-to-print')
    @transaction.atomic
    def send_to_print(self, request, pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)

        if order.status != Order.Status.READY:
            return Response({'detail': 'Order must be matched and have a design before sending to print'}, status=400)

        if not order.selected_carrier_code or not order.selected_service_code:
            return Response({'detail': 'Please select a shipping method first'}, status=400)

        if order.shipping_cost is None:
            return Response({'detail': 'Shipping rate not available yet — please retry rate selection'}, status=400)

        sub_user = request.user.sub_user_profile

        from .services import calculate_order_cost
        item_cost, extra_print_area_fee, shipping_cost, total_cost = calculate_order_cost(order)

        # Check balance
        if not sub_user.can_afford(total_cost):
            return Response({'detail': 'Insufficient balance'}, status=402)

        # Debit balance — the full order cost (product + extra print areas + shipping),
        # never just shipping alone, or production would be effectively free for the sub-user.
        from apps.accounts.models import BalanceTransaction
        sub_user_locked = type(sub_user).objects.select_for_update().get(pk=sub_user.pk)
        sub_user_locked.balance -= total_cost
        sub_user_locked.save(update_fields=['balance'])

        BalanceTransaction.objects.create(
            sub_user=sub_user_locked,
            type=BalanceTransaction.Type.DEBIT,
            amount=total_cost,
            item_amount=item_cost + extra_print_area_fee,
            shipping_amount=shipping_cost,
            order=order,
            order_reference=order.shipstation_order_id,
            description=f'Order {order.shipstation_order_number} sent to print',
            created_by=request.user,
        )

        order.status = Order.Status.SENT_TO_PRINT
        order.save(update_fields=['status'])
        log_event(
            order, OrderEvent.EventType.SENT_TO_PRINT, actor=request.user,
            description=f'Charged ${total_cost}',
        )

        return Response(OrderDetailSerializer(order).data)

    # ── Recall (pull back to New Orders before the PC has started production) ──

    @action(detail=True, methods=['post'], url_path='recall')
    @transaction.atomic
    def recall_to_new_orders(self, request, pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)

        if order.status != Order.Status.SENT_TO_PRINT:
            return Response(
                {'detail': 'Only orders waiting to be picked up for production can be recalled.'},
                status=400,
            )

        from .services import refund_order_debit
        refund_order_debit(
            order, request.user,
            f'Order {order.shipstation_order_number} recalled from print queue',
        )

        order.status = Order.Status.READY
        order.save(update_fields=['status'])
        log_event(order, OrderEvent.EventType.RECALLED, actor=request.user)

        return Response(OrderDetailSerializer(order).data)

    # ── Cancel ────────────────────────────────────────────────────────────────

    @action(detail=True, methods=['post'], url_path='cancel')
    @transaction.atomic
    def cancel(self, request, pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        if order.status in (Order.Status.IN_PRODUCTION, Order.Status.SHIPPED):
            return Response({'detail': 'Cannot cancel order in this status'}, status=400)

        if order.status == Order.Status.SENT_TO_PRINT:
            from .services import refund_order_debit
            refund_order_debit(order, request.user, f'Order {order.shipstation_order_number} cancelled')

        order.status = Order.Status.CANCELLED
        order.save(update_fields=['status'])
        log_event(order, OrderEvent.EventType.CANCELLED, actor=request.user)
        return Response(OrderDetailSerializer(order).data)

    # ── Hide (remove from platform) ───────────────────────────────────────────

    @action(detail=True, methods=['post'], url_path='hide')
    def hide(self, request, pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        order.is_hidden = True
        order.save(update_fields=['is_hidden'])
        log_event(order, OrderEvent.EventType.HIDDEN, actor=request.user)
        return Response({'detail': 'Order removed from platform.'})

    # ── PC: shipping override ─────────────────────────────────────────────────

    @action(detail=True, methods=['patch'], url_path='shipping-override',
            permission_classes=[IsPrintCenter])
    def shipping_override(self, request, pk=None):
        order = self.get_object()
        pc = request.user.print_center_profile
        if order.store.shipstation_account.print_center_id != pc.pk:
            raise PermissionDenied()

        ser = ShippingOverrideSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        if data.get('clear'):
            order.shipping_override = None
        else:
            override = {}
            if data.get('weight_oz') is not None:
                override['weight_oz'] = float(data['weight_oz'])
            if all(data.get(k) is not None for k in ('length', 'width', 'height')):
                override['length'] = float(data['length'])
                override['width']  = float(data['width'])
                override['height'] = float(data['height'])
            if data.get('note') is not None:
                override['note'] = data['note']
            order.shipping_override = override or None

        order.save(update_fields=['shipping_override'])
        return Response(OrderDetailSerializer(order).data)

    @action(detail=True, methods=['patch'], url_path='gift')
    def update_gift(self, request, pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        if 'is_gift' in request.data:
            order.is_gift = bool(request.data['is_gift'])
        if 'gift_message' in request.data:
            order.gift_message = request.data['gift_message'] or ''
        order.save(update_fields=['is_gift', 'gift_message'])
        return Response({'is_gift': order.is_gift, 'gift_message': order.gift_message})

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _assert_sub_user_owns(self, request, order):
        sub = getattr(request.user, 'sub_user_profile', None)
        if not sub:
            raise PermissionDenied('Sub-user access only.')

    def _refresh_order_status(self, order, actor=None):
        refresh_order_status(order, actor=actor)
        if order_is_matched(order):
            import logging
            import threading
            _log = logging.getLogger(__name__)
            def _push():
                from django.db import close_old_connections
                close_old_connections()
                from apps.shipping.tasks import (
                    push_order_shipping_to_shipstation,
                    refresh_shipping_rate_for_order,
                )
                try:
                    push_order_shipping_to_shipstation(order.pk)
                except Exception as exc:
                    _log.exception('ShipStation shipping push FAILED for order %s: %s', order.pk, exc)
                try:
                    refresh_shipping_rate_for_order(order.pk)
                except Exception as exc:
                    _log.exception('ShipStation rate refresh FAILED for order %s: %s', order.pk, exc)
            threading.Thread(target=_push, daemon=True).start()


# ── Bulk auto-match ───────────────────────────────────────────────────────────

class AutoMatchAllView(APIView):
    """Apply auto product-matching to every unmatched order for the authenticated sub-user."""
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        sub_user = _get_sub_user(request)
        if not sub_user:
            raise PermissionDenied('Sub-user access only.')

        unmatched_orders = (
            Order.objects.filter(sub_user=sub_user, is_hidden=False)
            .filter(status__in=[Order.Status.NEW, Order.Status.MATCHING, Order.Status.READY])
            .filter(items__pc_variant__isnull=True, items__unit_price__gte=0)
            .distinct()
        )
        total_matched = 0
        for order in unmatched_orders:
            matched = auto_match_order(order, actor=request.user)
            total_matched += matched
            if matched:
                self._push_shipping(order)
        return Response({'matched': total_matched})

    def _push_shipping(self, order):
        if not order_is_matched(order):
            return
        import threading
        def _push():
            from django.db import close_old_connections
            close_old_connections()
            from apps.shipping.tasks import (
                push_order_shipping_to_shipstation,
                refresh_shipping_rate_for_order,
            )
            try:
                push_order_shipping_to_shipstation(order.pk)
            except Exception:
                pass
            try:
                refresh_shipping_rate_for_order(order.pk)
            except Exception:
                pass
        threading.Thread(target=_push, daemon=True).start()


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


class MappingTemplateView(APIView):
    """Download a blank Excel template for product mappings."""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        import io
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        from django.http import HttpResponse

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Mappings'

        mkt_fill  = PatternFill(fill_type='solid', fgColor='DBEAFE')
        cat_fill  = PatternFill(fill_type='solid', fgColor='DCFCE7')
        bold      = Font(bold=True)
        gray_italic = Font(italic=True, color='999999')

        headers = ['Catalog Product', 'Catalog Size', 'Catalog Color',
                   'Option 1 Name', 'Option 1 Value', 'Option 2 Name', 'Option 2 Value']
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = bold
            cell.fill = cat_fill if col <= 3 else mkt_fill
            cell.alignment = Alignment(horizontal='center')

        examples = [
            ['Comfort Colors 1717 Unisex Shirt', 'M', 'Sea Foam', 'Item & Size', 'Unisex T-Shirt - M', 'Clothing Color', 'Sea Foam'],
            ['Comfort Colors 1717 Unisex Shirt', 'M', 'Black',    'Item & Size', 'Unisex T-Shirt - M', 'Clothing Color', 'Black'],
            ['Classic Mug 11oz',                 '11oz', 'White', 'Style',       'Classic Mug',        '',               ''],
        ]
        for r, ex in enumerate(examples, 2):
            for col, val in enumerate(ex, 1):
                cell = ws.cell(row=r, column=col, value=val)
                cell.font = gray_italic

        for col, w in enumerate([36, 12, 24, 18, 28, 18, 28], 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = w

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        resp = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        resp['Content-Disposition'] = 'attachment; filename="mapping_template.xlsx"'
        return resp


class MappingGenerateTemplateView(APIView):
    """Generate a mapping template pre-filled from the sub-user's unmatched orders."""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        import io
        from django.http import HttpResponse
        from .services import generate_mapping_template

        sub_user = _get_sub_user(request)
        wb = generate_mapping_template(sub_user)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        resp = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        resp['Content-Disposition'] = 'attachment; filename="mapping_template_orders.xlsx"'
        return resp


class MappingExportView(APIView):
    """Export all saved ProductMappings as a fully pre-filled Excel file."""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        import io
        from django.http import HttpResponse
        from .services import export_saved_mappings

        sub_user = _get_sub_user(request)
        wb = export_saved_mappings(sub_user)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        resp = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        resp['Content-Disposition'] = 'attachment; filename="saved_mappings.xlsx"'
        return resp


class MappingCountView(APIView):
    """Return the count of saved ProductMappings for the authenticated sub-user."""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        sub_user = _get_sub_user(request)
        count = ProductMapping.objects.filter(sub_user=sub_user).count()
        return Response({'count': count})


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

        created, updated, skipped, errors = parse_mapping_excel(file, sub_user, store=store)

        # Auto-match all unmatched order items (optional, controlled by caller)
        apply_to_orders = request.data.get('apply_to_orders', 'true').lower() not in ('false', '0', 'no')
        auto_matched = 0
        if apply_to_orders:
            from .services import auto_match_order
            unmatched_orders = (
                Order.objects.filter(sub_user=sub_user, is_hidden=False)
                .filter(status__in=[Order.Status.NEW, Order.Status.MATCHING, Order.Status.READY])
                .filter(items__pc_variant__isnull=True)
                .distinct()
            )
            auto_matched = sum(auto_match_order(o, actor=request.user) for o in unmatched_orders)

        return Response({
            'created': created,
            'updated': updated,
            'skipped': skipped,
            'errors':  errors[:20],
            'auto_matched': auto_matched,
        })


# ── Print Center — production queue view ──────────────────────────────────────

class ProductionQueueView(generics.ListAPIView):
    """Orders sent to print, visible to the print center for processing."""
    permission_classes = (IsPrintCenter,)
    serializer_class = OrderListSerializer
    filterset_fields = ('sub_user', 'store')

    def get_queryset(self):
        # Orders already in a batch that is still being labeled/built are not up for
        # selection again — otherwise they could land in two batches at once.
        from apps.production.batch_flow import OPEN_BATCH_STATUSES
        from apps.production.models import BatchOrder

        in_open_batch = (
            BatchOrder.objects.filter(batch__status__in=OPEN_BATCH_STATUSES)
            .exclude(label_status=BatchOrder.LabelStatus.REMOVED)
            .values('order_id')
        )
        return (
            Order.objects.filter(
                store__shipstation_account__print_center=self.request.user.print_center_profile,
                status=Order.Status.SENT_TO_PRINT,
            ).exclude(pk__in=in_open_batch)
            .select_related('store', 'sub_user__user')
            .prefetch_related('items')
            .order_by('created_at')
        )
