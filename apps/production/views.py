from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsPrintCenter
from apps.orders.models import Order

from .batch_flow import advance, preflight, run_in_background, run_label_pass, run_layout
from .models import BatchOrder, ProductionBatch
from .serializers import (
    CreateBatchSerializer,
    PreflightSerializer,
    ProductionBatchListSerializer,
    ProductionBatchSerializer,
)
from .tasks import label_batch, layout_batch


def _pc_orders(request, order_ids):
    """This print center's orders among order_ids, ready for the pre-flight checks."""
    return (
        Order.objects.filter(
            pk__in=order_ids,
            store__shipstation_account__print_center=request.user.print_center_profile,
        )
        .select_related('store__shipstation_account__print_center')
        .prefetch_related('items__designs', 'items__pc_variant__pc_product__print_areas',
                          'items__pc_variant__dimension_rules')
    )


class PreflightView(APIView):
    """Free checks for a selection of orders, before any label is bought."""
    permission_classes = (IsPrintCenter,)

    def post(self, request):
        serializer = PreflightSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        orders = list(_pc_orders(request, data['order_ids']))
        found = {o.pk for o in orders}
        result = preflight(orders, margin_in=float(data['margin_inches']))
        result['not_found'] = sorted(set(data['order_ids']) - found)
        return Response(result)


class BatchListCreateView(APIView):
    permission_classes = (IsPrintCenter,)

    def get(self, request):
        qs = ProductionBatch.objects.filter(
            print_center=request.user.print_center_profile
        ).order_by('-created_at')
        return Response(ProductionBatchListSerializer(qs, many=True).data)

    def post(self, request):
        serializer = CreateBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        pc = request.user.print_center_profile

        orders = list(_pc_orders(request, data['order_ids']))
        missing = set(data['order_ids']) - {o.pk for o in orders}
        if missing:
            return Response(
                {'detail': f'Orders not found: {sorted(missing)}'}, status=status.HTTP_400_BAD_REQUEST,
            )

        report = preflight(orders, margin_in=float(data['margin_inches']))
        if report['blocked_ids'] and not data['skip_invalid']:
            return Response(
                {'detail': f'{len(report["blocked_ids"])} order(s) have problems that must be fixed first.',
                 'preflight': report},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ok = set(report['ok_ids'])
        chosen = [o for o in orders if o.pk in ok]
        if not chosen:
            return Response(
                {'detail': 'None of the selected orders can be batched.', 'preflight': report},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            batch = ProductionBatch.objects.create(
                print_center=pc, created_by=request.user,
                sheet_length_inches=data['sheet_length_inches'],
                gang_sheet_dpi=data['gang_sheet_dpi'],
                margin_inches=data['margin_inches'],
                note=data.get('note', ''),
            )
            for order in sorted(chosen, key=lambda o: (o.ordered_at is None, o.ordered_at, o.pk)):
                BatchOrder.objects.create(batch=batch, order=order)

        run_in_background(label_batch, run_label_pass, batch.pk)

        body = ProductionBatchSerializer(batch).data
        body['left_out'] = report['blocked_ids']
        return Response(body, status=status.HTTP_201_CREATED)


class BatchDetailView(APIView):
    permission_classes = (IsPrintCenter,)

    def _get(self, request, pk):
        try:
            return ProductionBatch.objects.prefetch_related(
                'batch_orders__order__store', 'gang_sheets', 'slip_pdfs__gang_sheet'
            ).get(pk=pk, print_center=request.user.print_center_profile)
        except ProductionBatch.DoesNotExist:
            raise NotFound()

    def get(self, request, pk):
        return Response(ProductionBatchSerializer(self._get(request, pk)).data)

    def patch(self, request, pk):
        """Mark batch as COMPLETED."""
        batch = self._get(request, pk)
        if batch.status == ProductionBatch.Status.READY:
            batch.status = ProductionBatch.Status.COMPLETED
            batch.save(update_fields=['status'])
            order_ids = batch.batch_orders.filter(
                label_status=BatchOrder.LabelStatus.LABELED,
            ).values_list('order_id', flat=True)
            Order.objects.filter(pk__in=order_ids).update(status=Order.Status.SHIPPED)
        return Response(ProductionBatchSerializer(batch).data)


class BatchActionView(APIView):
    """Batch-level recovery actions.

    retry-failed  put every failed order back in the queue and buy their labels again
    continue      leave the unresolved orders out (they return to Sent to Print for a
                  later batch) and build the sheets from the ones that have labels
    rebuild       build the sheets/slips again from the labeled orders (free)
    """
    permission_classes = (IsPrintCenter,)

    def post(self, request, pk, action):
        try:
            batch = ProductionBatch.objects.get(pk=pk, print_center=request.user.print_center_profile)
        except ProductionBatch.DoesNotExist:
            raise NotFound()
        orders = BatchOrder.objects.filter(batch=batch)
        S = BatchOrder.LabelStatus

        if action == 'retry-failed':
            if batch.status not in (ProductionBatch.Status.NEEDS_ATTENTION, ProductionBatch.Status.FAILED):
                return Response({'detail': 'Nothing to retry in this state.'}, status=400)
            n = orders.filter(label_status=S.FAILED).update(label_status=S.PENDING, error_kind='', error_message='')
            # An order still waiting (e.g. after a stop) is picked up as well.
            if not n and not orders.filter(label_status=S.PENDING).exists():
                return Response({'detail': 'No failed orders to retry.'}, status=400)
            ProductionBatch.objects.filter(pk=pk).update(paused_reason='')
            run_in_background(label_batch, run_label_pass, batch.pk)

        elif action == 'continue':
            if batch.status != ProductionBatch.Status.NEEDS_ATTENTION:
                return Response({'detail': 'This batch is not waiting for a decision.'}, status=400)
            orders.exclude(label_status__in=(S.LABELED, S.REMOVED)).update(label_status=S.REMOVED)
            ProductionBatch.objects.filter(pk=pk).update(paused_reason='')
            advance(batch.pk)

        elif action == 'rebuild':
            if batch.status not in (ProductionBatch.Status.FAILED, ProductionBatch.Status.READY):
                return Response({'detail': 'Sheets can only be rebuilt for a finished or failed batch.'}, status=400)
            if not orders.filter(label_status=S.LABELED).exists():
                return Response({'detail': 'No labeled orders in this batch.'}, status=400)
            run_in_background(layout_batch, run_layout, batch.pk)

        else:
            raise NotFound()

        batch.refresh_from_db()
        return Response(ProductionBatchSerializer(batch).data)


class BatchOrderActionView(APIView):
    """Recovery actions for one order of a batch.

    retry      try this order's label again (only after a definite failure)
    reconcile  ask the provider whether a label was really created after an unknown
               outcome; adopts it if found, frees the order for retry if proven absent
    remove     pull the order out of the batch (it stays in Sent to Print)
    """
    permission_classes = (IsPrintCenter,)

    def post(self, request, pk, order_id, action):
        try:
            bo = BatchOrder.objects.select_related('batch', 'order').get(
                batch_id=pk, order_id=order_id, batch__print_center=request.user.print_center_profile,
            )
        except BatchOrder.DoesNotExist:
            raise NotFound()
        S = BatchOrder.LabelStatus
        batch = bo.batch
        if batch.status not in (ProductionBatch.Status.NEEDS_ATTENTION, ProductionBatch.Status.FAILED):
            return Response({'detail': 'This batch is not waiting for changes.'}, status=400)

        if action == 'retry':
            if bo.label_status != S.FAILED:
                return Response({'detail': 'Only a failed order can be retried.'}, status=400)
            bo.label_status, bo.error_kind, bo.error_message = S.PENDING, '', ''
            bo.save(update_fields=['label_status', 'error_kind', 'error_message'])
            run_in_background(label_batch, run_label_pass, batch.pk)

        elif action == 'reconcile':
            if bo.label_status != S.NEEDS_REVIEW:
                return Response({'detail': 'Only an order with an unknown outcome can be reconciled.'}, status=400)
            from apps.shipping.services import reconcile_attempt
            from .batch_flow import _apply_outcome
            _apply_outcome(bo, reconcile_attempt(bo.order, batch_order=bo))
            advance(batch.pk)

        elif action == 'remove':
            if bo.label_status == S.LABELED:
                return Response(
                    {'detail': 'This order already has a label — it cannot be removed from the batch.'}, status=400,
                )
            bo.label_status = S.REMOVED
            bo.save(update_fields=['label_status'])
            advance(batch.pk)

        else:
            raise NotFound()

        bo.refresh_from_db()
        batch.refresh_from_db()
        return Response(ProductionBatchSerializer(batch).data)
