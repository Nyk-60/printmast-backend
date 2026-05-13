from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.orders.models import Store
from apps.orders.serializers import StoreSerializer

from .serializers import RateRequestSerializer
from .services import apply_markup


class RatesView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        serializer = RateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # TODO: call ShipStationClient.estimate_rates with serializer.validated_data
        # Returning a placeholder shape so frontend can render the picker.
        base = {
            'standard': 4.50,
            'priority': 8.20,
            'express': 22.40,
        }
        rates = [
            {'service': code, 'service_label': code.title(), 'base_rate': rate, 'price': apply_markup(code, rate)}
            for code, rate in base.items()
        ]
        return Response({'rates': rates})


class StoresView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        stores = Store.objects.filter(owner=request.user, is_active=True)
        return Response(StoreSerializer(stores, many=True).data)
