from django.db import transaction
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Transaction
from .serializers import AddFundsSerializer, TransactionSerializer


class BalanceView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        return Response({'balance': request.user.balance})


class TransactionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TransactionSerializer
    permission_classes = (IsAuthenticated,)
    filterset_fields = ('tx_type', 'status')

    def get_queryset(self):
        return Transaction.objects.filter(owner=self.request.user)


class AddFundsView(APIView):
    permission_classes = (IsAuthenticated,)

    @transaction.atomic
    def post(self, request):
        serializer = AddFundsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        amount = serializer.validated_data['amount']

        tx = Transaction.objects.create(
            owner=request.user,
            tx_type='topup',
            amount=amount,
            status='pending',
            note='Funds top-up',
        )
        # TODO: integrate with payment provider (Stripe, etc.). On success:
        # tx.status = 'completed'; request.user.balance += amount; ...
        return Response(TransactionSerializer(tx).data)
