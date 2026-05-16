from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsPrintCenter
from apps.orders.models import Order

from .models import ProductionBatch, BatchOrder
from .serializers import CreateBatchSerializer, ProductionBatchSerializer
from .tasks import generate_batch


class BatchListCreateView(APIView):
    permission_classes = (IsPrintCenter,)

    def get(self, request):
        qs = ProductionBatch.objects.filter(
            print_center=request.user.print_center_profile
        ).prefetch_related('batch_orders', 'gang_sheets', 'slip_pdfs').order_by('-created_at')
        return Response(ProductionBatchSerializer(qs, many=True).data)

    def post(self, request):
        serializer = CreateBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        pc = request.user.print_center_profile

        # Validate orders belong to this print center and are ready
        orders = Order.objects.filter(
            pk__in=data['order_ids'],
            store__shipstation_account__print_center=pc,
            status=Order.Status.SENT_TO_PRINT,
        )
        found_ids = set(orders.values_list('pk', flat=True))
        missing = set(data['order_ids']) - found_ids
        if missing:
            return Response(
                {'detail': f'Orders not found or not ready: {sorted(missing)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch = ProductionBatch.objects.create(
            print_center=pc,
            created_by=request.user,
            sheet_length_inches=data['sheet_length_inches'],
            gang_sheet_dpi=data['gang_sheet_dpi'],
            margin_inches=data['margin_inches'],
            note=data.get('note', ''),
        )

        for seq, order in enumerate(orders.order_by('ordered_at'), start=1):
            BatchOrder.objects.create(batch=batch, order=order, sequence_number=seq)

        generate_batch.delay(batch.pk)

        return Response(
            ProductionBatchSerializer(batch).data,
            status=status.HTTP_201_CREATED,
        )


class BatchDetailView(APIView):
    permission_classes = (IsPrintCenter,)

    def _get(self, request, pk):
        try:
            return ProductionBatch.objects.prefetch_related(
                'batch_orders__order', 'gang_sheets', 'slip_pdfs__gang_sheet'
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
            # Update orders to SHIPPED
            order_ids = batch.batch_orders.values_list('order_id', flat=True)
            Order.objects.filter(pk__in=order_ids).update(status=Order.Status.SHIPPED)
        return Response(ProductionBatchSerializer(batch).data)
