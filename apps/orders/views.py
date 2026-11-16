from decimal import Decimal

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsPrintCenter
from apps.accounts.scoping import get_sub_user_scope
from apps.designs.models import OrderItemDesign, SkuDesign
from apps.designs.serializers import OrderItemDesignSerializer

from .models import Order, OrderEvent, OrderItem, OrderTag, ProductMapping
from .validators import validate_label_file
from .serializers import (
    AddressUpdateSerializer,
    MatchItemSerializer,
    OrderActivityLogSerializer,
    OrderDetailSerializer,
    OrderListSerializer,
    OrderTagSerializer,
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
    """The billing/parent sub-user — resolves the same for a real sub-user or
    a Team Member acting on their behalf. Safe to use for business logic on
    an already-scoped object (settings reads, balance checks/writes); does
    NOT apply a Team Member's store restriction — use _get_scope for that,
    wherever a fresh queryset is being built rather than a single object
    already fetched through a store-aware get_queryset()."""
    sub_user, _ = get_sub_user_scope(request.user)
    return sub_user


def _get_scope(request):
    """(sub_user, store_ids_or_None) — see apps.accounts.scoping.get_sub_user_scope.
    Use at every site that builds an Order (or other store-aware) queryset
    from scratch, so a Team Member's store restriction is never skipped."""
    return get_sub_user_scope(request.user)


def _exclude_open_batch_orders(qs):
    """An order stays `sent_to_print` for its whole time in a batch — labels are bought
    and sheets built before it ever flips to in_production — so without this it would
    still look like it's sitting untouched in Ready for Print while a batch is actively
    working on it (and could even be selected into a second batch; preflight would catch
    that, but it shouldn't even look available). Used by the list, tab-counts, and
    running-total endpoints so they all agree on what's actually still queued."""
    from apps.production.batch_flow import OPEN_BATCH_STATUSES
    from apps.production.models import BatchOrder

    in_open_batch = (
        BatchOrder.objects.filter(batch__status__in=OPEN_BATCH_STATUSES)
        .exclude(label_status=BatchOrder.LabelStatus.REMOVED)
        .values('order_id')
    )
    return qs.exclude(pk__in=in_open_batch)


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

        sub_user, store_ids = get_sub_user_scope(user)
        if sub_user is not None:
            qs = (
                Order.objects.filter(sub_user=sub_user, is_hidden=False)
                .select_related('store__shipstation_account__print_center', 'sub_user__user', 'tag')
                .prefetch_related('items__pc_variant__pc_product')
            )
            if store_ids is not None:
                qs = qs.filter(store_id__in=store_ids)
        elif user.is_print_center:
            qs = (
                Order.objects.filter(
                    store__shipstation_account__print_center=user.print_center_profile,
                    is_hidden=False,
                )
                .select_related('store', 'sub_user__user', 'tag')
                .prefetch_related('items__pc_variant__pc_product', 'items__designs')
            )
        else:
            return Order.objects.none()

        # Tab filtering (new_orders / ready_for_print / in_production / completed; 'active' = backward compat)
        if tab in ('new_orders', 'active'):
            qs = qs.filter(status__in=self.NEW_ORDER_STATUSES)
        elif tab == 'ready_for_print':
            qs = _exclude_open_batch_orders(qs.filter(status__in=self.READY_FOR_PRINT_STATUSES))
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
        sub_user, store_ids = get_sub_user_scope(user)
        if sub_user is not None:
            base_qs = Order.objects.filter(sub_user=sub_user, is_hidden=False)
            if store_ids is not None:
                base_qs = base_qs.filter(store_id__in=store_ids)
        elif user.is_print_center:
            base_qs = Order.objects.filter(
                store__shipstation_account__print_center=user.print_center_profile,
                is_hidden=False,
            )
        else:
            base_qs = Order.objects.none()

        # An order in an open batch is always still `sent_to_print` (see
        # _exclude_open_batch_orders) — safe to exclude globally here since no other
        # status bucket could contain one.
        qs = _exclude_open_batch_orders(self.filter_queryset(base_qs))
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
        sub_user, store_ids = get_sub_user_scope(user)
        if sub_user is not None:
            base_qs = Order.objects.filter(sub_user=sub_user, is_hidden=False)
            if store_ids is not None:
                base_qs = base_qs.filter(store_id__in=store_ids)
        elif user.is_print_center:
            base_qs = Order.objects.filter(
                store__shipstation_account__print_center=user.print_center_profile,
                is_hidden=False,
            )
        else:
            base_qs = Order.objects.none()

        qs = _exclude_open_batch_orders(self.filter_queryset(
            base_qs.filter(status__in=self.READY_FOR_PRINT_STATUSES)
        )).prefetch_related('items__pc_variant', 'items__designs')

        count, total = 0, Decimal('0')
        for order in qs:
            count += 1
            _, _, _, order_total = calculate_order_cost(order)
            total += order_total

        return Response({'count': count, 'total': total})

    # ── Product breakdown (PC: Ready for Print / In Production / Completed) ───────

    def _build_product_breakdown(self, request, tab):
        """What garments are actually sitting in this status bucket right now —
        classified by catalog product, then by size/color variant. Honors the
        same store/sub_user/search filters as the list endpoint; only products
        actually present in the filtered set are returned (nothing at zero).
        Shared by the on-screen JSON breakdown and the PDF export below, so
        they can never drift out of sync with each other. Each product also
        carries `_image_path` (local filesystem path, for the PDF to embed
        directly rather than re-fetching over HTTP) — stripped before the
        JSON response, since a server-side path has no business leaving it."""
        status_map = {
            'ready_for_print': self.READY_FOR_PRINT_STATUSES,
            'in_production':   self.IN_PRODUCTION_STATUSES,
            'completed':       self.COMPLETED_STATUSES,
        }
        statuses = status_map.get(tab)
        if not statuses:
            return None

        base_qs = Order.objects.filter(
            store__shipstation_account__print_center=request.user.print_center_profile,
            is_hidden=False,
        )
        qs = self.filter_queryset(base_qs).filter(status__in=statuses)
        if tab == 'ready_for_print':
            # Same reasoning as tab_counts/ready_for_print_summary: an order in an
            # open batch is still `sent_to_print` but no longer "waiting".
            qs = _exclude_open_batch_orders(qs)

        items = (
            OrderItem.objects.filter(order__in=qs, pc_variant__isnull=False, unit_price__gte=0)
            .select_related('pc_variant__pc_product')
        )

        products = {}
        for item in items:
            variant = item.pc_variant
            product = variant.pc_product
            bucket = products.setdefault(product.id, {
                'pc_product_id':  product.id,
                'product_name':   product.name,
                'image_url': (
                    request.build_absolute_uri(product.primary_image.url)
                    if product.primary_image else None
                ),
                '_image_path': product.primary_image.path if product.primary_image else None,
                'total_quantity': 0,
                'variants': {},
            })
            bucket['total_quantity'] += item.quantity
            vkey = (variant.size, variant.color)
            bucket['variants'][vkey] = bucket['variants'].get(vkey, 0) + item.quantity

        result = []
        for bucket in products.values():
            variants = sorted(
                (
                    {'size': size, 'color': color, 'quantity': qty}
                    for (size, color), qty in bucket['variants'].items()
                ),
                key=lambda v: -v['quantity'],
            )
            result.append({**bucket, 'variants': variants})
        result.sort(key=lambda p: -p['total_quantity'])
        return result

    @action(detail=False, methods=['get'], url_path='product-breakdown', permission_classes=[IsPrintCenter])
    def product_breakdown(self, request):
        tab = request.query_params.get('tab', '')
        result = self._build_product_breakdown(request, tab)
        if result is None:
            return Response(
                {'detail': 'tab must be one of: ready_for_print, in_production, completed'}, status=400,
            )
        return Response([{k: v for k, v in p.items() if k != '_image_path'} for p in result])

    @action(detail=False, methods=['get'], url_path='product-breakdown/pdf', permission_classes=[IsPrintCenter])
    def product_breakdown_pdf(self, request):
        """Same data as product_breakdown, exported as a "Supply List" PDF —
        a same-day reference grouped by product, card-style like the on-screen
        panel. Which units are already in the PC's own stock vs. need same-day
        sourcing isn't tracked anywhere — the PC decides that themselves."""
        tab = request.query_params.get('tab', '')
        result = self._build_product_breakdown(request, tab)
        if result is None:
            return Response(
                {'detail': 'tab must be one of: ready_for_print, in_production, completed'}, status=400,
            )

        from django.http import HttpResponse

        from .breakdown_pdf import generate_breakdown_pdf

        filter_bits = []
        store_id = request.query_params.get('store')
        if store_id:
            from apps.shipping.models import Store
            store = Store.objects.filter(pk=store_id).first()
            if store:
                filter_bits.append(f'Store: {store.name}')
        sub_user_id = request.query_params.get('sub_user')
        if sub_user_id:
            from apps.accounts.models import SubUserProfile
            su = SubUserProfile.objects.filter(pk=sub_user_id).select_related('user').first()
            if su:
                filter_bits.append(f'Seller: {su.user.full_name or su.user.email}')

        pdf_bytes = generate_breakdown_pdf(
            result,
            tab_label={'ready_for_print': 'Ready for Print', 'in_production': 'In Production',
                       'completed': 'Completed'}[tab],
            filter_label=' · '.join(filter_bits),
        )
        resp = HttpResponse(pdf_bytes, content_type='application/pdf')
        resp['Content-Disposition'] = 'attachment; filename="supply-list.pdf"'
        return resp

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
                    sub_user=_get_sub_user(request),
                    store=order.store,
                    marketplace_sku=item.marketplace_sku,
                    marketplace_variant=_variant_key(item.raw_options or {}),
                    defaults={'pc_variant_id': entry['pc_variant_id']},
                )

            # Try to auto-match designs after product is matched
            if _get_sub_user(request).auto_design_matching:
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

        from apps.shipping.tasks import push_personalization_to_shipstation
        sync_error = self._sync_to_shipstation(
            order, lambda: push_personalization_to_shipstation(order.pk, item.pk), request,
        )

        return Response({'personalization': item.personalization, 'shipstation_sync_error': sync_error})

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
            context={'order_item': item, 'sub_user': _get_sub_user(request), 'request': request},
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
        # The file may be shared with a SkuDesign library entry, or with another
        # order that auto-matched the same design (see designs.models.file_in_use) —
        # only remove the bytes once nothing else still points at them.
        from apps.designs.models import file_in_use

        if design.file and not file_in_use(design.file.name, exclude_order_item_design_id=design.pk):
            design.file.delete(save=False)
        design.delete()
        self._refresh_order_status(order, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['delete'], url_path='items/(?P<item_pk>[0-9]+)/library-design/(?P<sku_design_pk>[0-9]+)')
    def delete_library_design(self, request, pk=None, item_pk=None, sku_design_pk=None):
        """Delete a SkuDesign (library entry) and its associated OrderItemDesigns in this order."""
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        sub_user = _get_sub_user(request)
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

        # Other orders may have auto-matched this exact design before this one removed
        # it — their OrderItemDesign rows share the same physical file (see
        # designs.models.file_in_use) and must keep working. Only delete the bytes
        # once nothing else still references them.
        from apps.designs.models import file_in_use

        if sku_design.file and not file_in_use(sku_design.file.name, exclude_sku_design_id=sku_design.pk):
            sku_design.file.delete(save=False)
        sku_design.delete()

        self._refresh_order_status(order, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # ── Shipping ──────────────────────────────────────────────────────────────

    @action(detail=True, methods=['post'], url_path='upload-label', parser_classes=[MultiPartParser])
    def upload_label(self, request, pk=None):
        """The seller already bought this order's label elsewhere (e.g. directly on
        Etsy) and is attaching it themselves. labeled_at being set makes
        purchase_label_for_order() skip buying a real one — see apps.shipping.services."""
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        self._assert_not_labeled(order)
        if order.status in (
            Order.Status.SHIPPED, Order.Status.DELIVERED, Order.Status.CANCELLED, Order.Status.REFUNDED,
        ):
            raise ValidationError({'detail': 'This order can no longer be edited.'})

        file = request.FILES.get('label')
        if not file:
            return Response({'detail': 'No file provided'}, status=400)
        validate_label_file(file)

        order.label_file = file
        order.label_source = Order.LabelSource.UPLOADED
        order.labeled_at = timezone.now()
        order.save(update_fields=['label_file', 'label_source', 'labeled_at'])

        log_event(
            order, OrderEvent.EventType.LABEL_CONFIRMED, actor=request.user,
            description=f'Shipping label uploaded by seller ({file.name})',
        )
        return Response(OrderDetailSerializer(order).data)

    @action(detail=True, methods=['get'], url_path='label-preview')
    def label_preview(self, request, pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        from apps.shipping.labels import load_label_png
        png = load_label_png(order, orientation='portrait')
        if not png:
            raise NotFound()
        return HttpResponse(png, content_type='image/png')

    @action(detail=True, methods=['delete'], url_path='label')
    def remove_label(self, request, pk=None):
        """Undo an upload_label — only for a seller-uploaded label; a purchased
        one must be voided with the provider, not deleted here."""
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        if order.label_source != Order.LabelSource.UPLOADED:
            raise ValidationError({'detail': 'Only an uploaded label can be removed here.'})

        if order.label_file:
            order.label_file.delete(save=False)
        order.label_file = None
        order.label_source = Order.LabelSource.PURCHASED
        order.labeled_at = None
        order.save(update_fields=['label_file', 'label_source', 'labeled_at'])

        log_event(order, OrderEvent.EventType.LABEL_REMOVED, actor=request.user,
                  description='Uploaded shipping label removed')
        return Response(OrderDetailSerializer(order).data)

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

    @action(detail=True, methods=['patch'], url_path='shipping-category')
    def set_shipping_category(self, request, pk=None):
        """Manually upgrade/downgrade the shipping speed (Standard/Priority/Express)
        for one order — either the owning sub-user or the print center can do this
        (get_object() already scopes to whichever the requester actually owns).

        Purely internal: never changes requested_service, so ShipStation/the
        marketplace still shows whatever the customer actually ordered. Clears the
        current selection so a fresh rate for the new category is picked right away.
        """
        order = self.get_object()

        if order.status not in self.NEW_ORDER_STATUSES:
            return Response(
                {'detail': 'Shipping can only be changed before the order is sent to print.'},
                status=400,
            )

        category = request.data.get('category') or ''
        if category and category not in dict(Order.ShippingCategory.choices):
            return Response({'detail': f'Unknown category "{category}".'}, status=400)

        order.shipping_category_override = category
        order.save(update_fields=['shipping_category_override'])
        log_event(
            order, OrderEvent.EventType.SHIPPING_CATEGORY_CHANGED, actor=request.user,
            description=f'Shipping category set to "{category or "default"}"',
        )

        # Pick a fresh rate for the new category right away instead of leaving the
        # order rate-less until the sub-user happens to reopen the rate dropdown.
        self._trigger_rate_reselect(order)

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

        if order.shipstation_sync_error:
            return Response(
                {'detail': f'This order has an unresolved ShipStation sync issue: {order.shipstation_sync_error}'},
                status=400,
            )

        sub_user = _get_sub_user(request)

        from .services import calculate_order_cost, calculate_order_cost_lines
        item_cost, extra_print_area_fee, shipping_cost, total_cost = calculate_order_cost(order)

        # A PC-fault reship order is never billed to the sub-user — skip the
        # balance check and zero every line below instead of charging the
        # normal, freshly-computed cost. See apps/reship/services.py.
        if order.is_free_reship:
            total_cost = Decimal('0')
        elif not sub_user.can_afford(total_cost):
            return Response({'detail': 'Insufficient balance'}, status=402)

        # Debit balance — the full order cost (product + extra print areas + shipping),
        # never just shipping alone, or production would be effectively free for the sub-user.
        from apps.accounts.models import BalanceTransaction
        sub_user_locked = type(sub_user).objects.select_for_update().get(pk=sub_user.pk)
        sub_user_locked.balance -= total_cost
        sub_user_locked.save(update_fields=['balance'])

        order_number = order.shipstation_order_number or order.external_order_number

        if order.is_free_reship:
            from apps.reship.models import ReshipRequest
            reship = ReshipRequest.objects.filter(new_order=order).first()
            reason_label = reship.get_reason_display() if reship and reship.reason else 'a print center error'
            description = f'Reshipment due to {reason_label}'
        else:
            description = f'Order {order.shipstation_order_number} sent to print'

        for line in calculate_order_cost_lines(order):
            is_shipping = line['category'] == 'shipping'
            line_amount = Decimal('0') if order.is_free_reship else line['amount']
            BalanceTransaction.objects.create(
                sub_user=sub_user_locked,
                type=BalanceTransaction.Type.DEBIT,
                amount=line_amount,
                item_amount=Decimal('0') if is_shipping else line_amount,
                shipping_amount=line_amount if is_shipping else Decimal('0'),
                category=line['category'],
                quantity=line.get('quantity', 1),
                product_name=line['product_name'], size=line['size'], color=line['color'],
                order=order,
                order_reference=order.shipstation_order_id,
                order_number=order_number,
                description=description,
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
        if order.status not in self.NEW_ORDER_STATUSES:
            return Response(
                {'detail': 'An order that has already been sent to print can no longer be removed from the platform.'},
                status=400,
            )
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

    @action(detail=True, methods=['post'], url_path='resync-shipstation')
    def resync_shipstation(self, request, pk=None):
        """Retry button for the 'Not synced to ShipStation' badge — full
        resync (apps/shipping/client.py::resync_order), not a replay of
        whichever specific edit failed, so it's safe to call regardless of
        what originally went wrong."""
        order = self.get_object()
        self._assert_sub_user_owns(request, order)

        from apps.shipping.tasks import resync_order_to_shipstation
        sync_error = self._sync_to_shipstation(
            order, lambda: resync_order_to_shipstation(order.pk), request,
        )
        return Response({'shipstation_sync_error': sync_error})

    @action(detail=True, methods=['patch'], url_path='notes')
    def update_notes(self, request, pk=None):
        """Buyer note / internal note / gift — all order-level, edited together
        from one panel. Pushed to ShipStation (customerNotes/internalNotes/
        giftMessage/gift), unlike the old gift-only version of this action."""
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        self._assert_order_editable(order)

        NOTE_MAX_LENGTH = 350
        for field in ('gift_message', 'buyer_note', 'internal_note'):
            if field in request.data and len(request.data[field] or '') > NOTE_MAX_LENGTH:
                return Response({'detail': f'{field.replace("_", " ").title()} must be {NOTE_MAX_LENGTH} characters or fewer.'}, status=400)

        fields = []
        if 'is_gift' in request.data:
            order.is_gift = bool(request.data['is_gift'])
            fields.append('is_gift')
        if 'gift_message' in request.data:
            order.gift_message = request.data['gift_message'] or ''
            fields.append('gift_message')
        if 'buyer_note' in request.data:
            order.buyer_note = request.data['buyer_note'] or ''
            fields.append('buyer_note')
        if 'internal_note' in request.data:
            order.internal_note = request.data['internal_note'] or ''
            fields.append('internal_note')
        if fields:
            order.save(update_fields=fields)

        from apps.shipping.tasks import push_notes_to_shipstation
        sync_error = self._sync_to_shipstation(order, lambda: push_notes_to_shipstation(order.pk), request)

        return Response({
            'is_gift': order.is_gift, 'gift_message': order.gift_message,
            'buyer_note': order.buyer_note, 'internal_note': order.internal_note,
            'shipstation_sync_error': sync_error,
        })

    @action(detail=True, methods=['patch'], url_path='tag')
    def set_tag(self, request, pk=None):
        """Apply/clear a tracking tag — platform-only (never pushed to
        ShipStation), open to both the sub-user and their team members
        (unlike tag definitions themselves, which only the sub-user manages;
        see OrderTagListCreateView/OrderTagDetailView). Not gated on order
        status: a tag is purely informational and never affects billing."""
        order = self.get_object()
        self._assert_sub_user_owns(request, order)

        tag_id = request.data.get('tag_id')
        if tag_id is None:
            order.tag = None
        else:
            try:
                order.tag = order.sub_user.order_tags.get(pk=tag_id)
            except OrderTag.DoesNotExist:
                raise NotFound('Tag not found.')
        order.save(update_fields=['tag'])
        return Response(OrderTagSerializer(order.tag).data if order.tag else None)

    @action(detail=True, methods=['patch'], url_path='items/(?P<item_pk>[0-9]+)/quantity')
    def update_quantity(self, request, pk=None, item_pk=None):
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        self._assert_order_editable(order)
        self._assert_rated(order)
        try:
            item = order.items.get(pk=item_pk)
        except OrderItem.DoesNotExist:
            raise NotFound()
        if not item.pc_variant_id:
            return Response({'detail': 'Match this item to a product first.'}, status=400)

        try:
            quantity = int(request.data.get('quantity'))
        except (TypeError, ValueError):
            return Response({'detail': 'quantity must be a positive integer.'}, status=400)
        if quantity < 1:
            return Response({'detail': 'quantity must be a positive integer.'}, status=400)

        item.quantity = quantity
        item.save(update_fields=['quantity'])

        from apps.shipping.tasks import push_quantity_to_shipstation
        sync_error = self._sync_to_shipstation(
            order, lambda: push_quantity_to_shipstation(order.pk, item.pk), request,
        )

        self._trigger_rate_reselect(order)
        return Response({'quantity': item.quantity, 'shipstation_sync_error': sync_error})

    @action(detail=True, methods=['patch'], url_path='items/(?P<item_pk>[0-9]+)/sku')
    def update_sku(self, request, pk=None, item_pk=None):
        """SKU is purely descriptive — doesn't touch cost/weight, so unlike
        quantity it isn't gated on the item being matched or the order rated."""
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        self._assert_order_editable(order)
        try:
            item = order.items.get(pk=item_pk)
        except OrderItem.DoesNotExist:
            raise NotFound()

        item.marketplace_sku = (request.data.get('sku') or '').strip()
        item.save(update_fields=['marketplace_sku'])

        from apps.shipping.tasks import push_sku_to_shipstation
        sync_error = self._sync_to_shipstation(order, lambda: push_sku_to_shipstation(order.pk, item.pk), request)

        return Response({'marketplace_sku': item.marketplace_sku, 'shipstation_sync_error': sync_error})

    @action(detail=True, methods=['post'], url_path='items')
    def add_item(self, request, pk=None):
        """Add a brand-new item (or 'clone' — same mechanism, per the user's own
        description: pick a catalog product, its image becomes the item image, a
        generic editable placeholder stands in for a marketplace title)."""
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        self._assert_order_editable(order)
        self._assert_rated(order)

        from apps.catalog.models import PrintCenterVariant
        pc_variant_id = request.data.get('pc_variant_id')
        try:
            variant = PrintCenterVariant.objects.select_related('pc_product').get(
                pk=pc_variant_id, pc_product__print_center=order.store.shipstation_account.print_center,
            )
        except (PrintCenterVariant.DoesNotExist, ValueError, TypeError):
            return Response({'detail': 'Invalid product.'}, status=400)

        try:
            quantity = int(request.data.get('quantity', 1))
        except (TypeError, ValueError):
            quantity = 1
        quantity = max(1, quantity)
        name = (request.data.get('name') or 'Manually Added Item').strip() or 'Manually Added Item'
        sku = (request.data.get('sku') or '').strip()

        image_url = ''
        if variant.pc_product.primary_image:
            image_url = request.build_absolute_uri(variant.pc_product.primary_image.url)

        item = OrderItem.objects.create(
            order=order, is_manual=True, pc_variant=variant,
            raw_product_name=name, marketplace_sku=sku, image_url=image_url,
            quantity=quantity, unit_price=Decimal('0'),
            matched_at=timezone.now(),
        )
        log_event(
            order, OrderEvent.EventType.MATCHED, actor=request.user,
            description=f'Item added manually: "{name}"',
        )

        from apps.shipping.tasks import push_new_item_to_shipstation
        sync_error = self._sync_to_shipstation(
            order, lambda: push_new_item_to_shipstation(order.pk, item.pk), request,
        )

        self._trigger_rate_reselect(order)
        from .serializers import OrderItemPreviewSerializer
        data = OrderItemPreviewSerializer(item, context={'request': request}).data
        data['shipstation_sync_error'] = sync_error
        return Response(data, status=201)

    @action(detail=True, methods=['post'], url_path='items/(?P<item_pk>[0-9]+)/clone')
    def clone_item(self, request, pk=None, item_pk=None):
        """True duplicate — same product/variant/personalization/SKU/image as the
        source item, one extra unit added as its own line (distinct from Add Item,
        which opens the catalog picker for a genuinely different product)."""
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        self._assert_order_editable(order)
        self._assert_rated(order)
        try:
            source = order.items.get(pk=item_pk)
        except OrderItem.DoesNotExist:
            raise NotFound()
        if not source.pc_variant_id:
            return Response({'detail': 'Match this item to a product first.'}, status=400)

        item = OrderItem.objects.create(
            order=order, is_manual=True, pc_variant_id=source.pc_variant_id,
            raw_product_name=source.raw_product_name, raw_options=source.raw_options,
            personalization=source.personalization, marketplace_sku=source.marketplace_sku,
            image_url=source.image_url, quantity=1, unit_price=Decimal('0'),
            matched_at=timezone.now(),
        )
        log_event(
            order, OrderEvent.EventType.MATCHED, actor=request.user,
            description=f'Item cloned: "{source.raw_product_name}"',
        )

        from apps.shipping.tasks import push_new_item_to_shipstation
        sync_error = self._sync_to_shipstation(
            order, lambda: push_new_item_to_shipstation(order.pk, item.pk), request,
        )

        self._trigger_rate_reselect(order)
        from .serializers import OrderItemPreviewSerializer
        data = OrderItemPreviewSerializer(item, context={'request': request}).data
        data['shipstation_sync_error'] = sync_error
        return Response(data, status=201)

    @action(detail=True, methods=['delete'], url_path='items/(?P<item_pk>[0-9]+)')
    def delete_item(self, request, pk=None, item_pk=None):
        """An order must always keep at least one real (non-adjustment) item."""
        order = self.get_object()
        self._assert_sub_user_owns(request, order)
        self._assert_order_editable(order)
        try:
            item = order.items.get(pk=item_pk)
        except OrderItem.DoesNotExist:
            raise NotFound()

        if item.unit_price >= 0 and order.items.filter(unit_price__gte=0).count() <= 1:
            return Response({'detail': 'An order must have at least one item.'}, status=400)

        ss_item_id = item.shipstation_item_id
        name = item.raw_product_name
        item.delete()
        log_event(
            order, OrderEvent.EventType.MATCHED, actor=request.user,
            description=f'Item removed: "{name}"',
        )

        from apps.shipping.tasks import push_item_removal_to_shipstation
        sync_error = self._sync_to_shipstation(
            order, lambda: push_item_removal_to_shipstation(order.pk, ss_item_id), request,
        )

        self._trigger_rate_reselect(order)
        return Response({'deleted': True, 'shipstation_sync_error': sync_error})

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _assert_sub_user_owns(self, request, order):
        sub = _get_sub_user(request)
        if not sub:
            raise PermissionDenied('Sub-user access only.')

    def _assert_order_editable(self, order):
        if order.status not in self.NEW_ORDER_STATUSES:
            raise ValidationError({'detail': 'Order content can only be changed before it is sent to print.'})

    def _assert_rated(self, order):
        if order.shipping_cost is None:
            raise ValidationError({'detail': 'Match this item and get a shipping rate first.'})

    def _assert_not_labeled(self, order):
        if order.labeled_at is not None:
            raise ValidationError({'detail': 'This order already has a label.'})

    def _sync_to_shipstation(self, order, push_fn, request):
        """Runs a ShipStation push synchronously (right in the request/response
        cycle, like apps/reship/services.py::_push_to_shipstation) so a failure
        is never silent: it's returned to the caller immediately, persisted on
        the order (shipstation_sync_error — shown as a badge even after a
        refresh), and raises an in-app Notification so it isn't missed even if
        the user navigates away before reading the response.

        Returns the error string (empty on success) — callers fold it into
        their response body as 'shipstation_sync_error'.
        """
        try:
            push_fn()
            if order.shipstation_sync_error:
                order.shipstation_sync_error = ''
                order.save(update_fields=['shipstation_sync_error'])
            return ''
        except Exception as exc:
            msg = str(exc)[:500]
            order.shipstation_sync_error = msg
            order.save(update_fields=['shipstation_sync_error'])
            sub = _get_sub_user(request)
            if sub:
                from apps.accounts.models import Notification
                Notification.objects.create(
                    user=sub.user,
                    title='Not synced to ShipStation',
                    body=f'Order {order.shipstation_order_number or order.external_order_number}: {msg}',
                    link='/orders',
                )
            return msg

    def _trigger_rate_reselect(self, order):
        """Clear the current carrier/service selection and fire an async
        auto-select — used after anything that can change the order's weight
        (quantity edit, item added), same as set_shipping_category already
        does for a category change."""
        order.selected_carrier_code = ''
        order.selected_service_code = ''
        order.shipping_cost = None
        order.shipping_cost_raw = None
        order.save(update_fields=['selected_carrier_code', 'selected_service_code', 'shipping_cost', 'shipping_cost_raw'])

        import logging
        import threading

        def _reselect():
            from django.db import close_old_connections
            close_old_connections()
            from apps.shipping.tasks import auto_select_shipping_for_order
            try:
                auto_select_shipping_for_order(order.pk)
            except Exception:
                logging.getLogger(__name__).exception(
                    'Auto-select after content change failed for order %s', order.pk,
                )
        threading.Thread(target=_reselect, daemon=True).start()

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
        sub_user, store_ids = _get_scope(request)
        if not sub_user:
            raise PermissionDenied('Sub-user access only.')

        unmatched_orders = (
            Order.objects.filter(sub_user=sub_user, is_hidden=False)
            .filter(status__in=[Order.Status.NEW, Order.Status.MATCHING, Order.Status.READY])
            .filter(items__pc_variant__isnull=True, items__unit_price__gte=0)
            .distinct()
        )
        if store_ids is not None:
            unmatched_orders = unmatched_orders.filter(store_id__in=store_ids)
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

class OrderTagListCreateView(generics.ListCreateAPIView):
    """Tag definitions (name + color) a sub-user tracks orders with — e.g.
    "Waiting on customer". List is open to the sub-user and their team
    members (so both can see the options to apply); creating a new tag is
    sub-user-only — a team member only ever applies/removes an existing one
    on an order (see OrderViewSet.set_tag)."""
    permission_classes = (IsAuthenticated,)
    serializer_class = OrderTagSerializer

    def get_queryset(self):
        sub_user, _ = get_sub_user_scope(self.request.user)
        if not sub_user:
            return OrderTag.objects.none()
        return sub_user.order_tags.all()

    def perform_create(self, serializer):
        if getattr(self.request.user, 'sub_user_profile', None) is None:
            raise PermissionDenied('Only the account owner can create tags.')
        serializer.save(sub_user=self.request.user.sub_user_profile)


class OrderTagDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = OrderTagSerializer

    def get_queryset(self):
        sub_user, _ = get_sub_user_scope(self.request.user)
        if not sub_user:
            return OrderTag.objects.none()
        return sub_user.order_tags.all()

    def _assert_owner(self):
        if getattr(self.request.user, 'sub_user_profile', None) is None:
            raise PermissionDenied('Only the account owner can manage tags.')

    def perform_update(self, serializer):
        self._assert_owner()
        serializer.save()

    def perform_destroy(self, instance):
        self._assert_owner()
        instance.delete()


class ProductMappingListView(generics.ListAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = ProductMappingSerializer
    search_fields = ('marketplace_sku', 'marketplace_variant')

    def get_queryset(self):
        sub_user, store_ids = _get_scope(self.request)
        qs = ProductMapping.objects.filter(sub_user=sub_user)
        if store_ids is not None:
            qs = qs.filter(store_id__in=store_ids)
        return qs.select_related('pc_variant', 'store').order_by('-created_at')


class ProductMappingDeleteView(generics.DestroyAPIView):
    permission_classes = (IsAuthenticated,)

    def get_object(self):
        sub_user, store_ids = _get_scope(self.request)
        try:
            qs = ProductMapping.objects.filter(sub_user=sub_user)
            if store_ids is not None:
                qs = qs.filter(store_id__in=store_ids)
            return qs.get(pk=self.kwargs['pk'])
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

        sub_user, store_ids = _get_scope(request)
        wb = generate_mapping_template(sub_user, store_ids=store_ids)

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

        sub_user, store_ids = _get_scope(request)
        wb = export_saved_mappings(sub_user, store_ids=store_ids)

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
        sub_user, store_ids = _get_scope(request)
        qs = ProductMapping.objects.filter(sub_user=sub_user)
        if store_ids is not None:
            qs = qs.filter(store_id__in=store_ids)
        return Response({'count': qs.count()})


class MappingUploadView(APIView):
    """Upload an Excel file to bulk-create/update ProductMappings."""
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        file = request.FILES.get('file')
        if not file:
            return Response({'detail': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)

        sub_user, store_ids = _get_scope(request)
        store_id = request.data.get('store_id')
        store = None
        if store_id:
            if store_ids is not None and int(store_id) not in store_ids:
                return Response({'detail': 'Store not found or not assigned to you'}, status=400)
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
            if store_ids is not None:
                unmatched_orders = unmatched_orders.filter(store_id__in=store_ids)
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
            .select_related('store', 'sub_user__user', 'tag')
            .prefetch_related('items')
            .order_by('created_at')
        )
