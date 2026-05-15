from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import SubUserProfile
from apps.accounts.permissions import IsPrintCenter

from .client import ShipStationClient, ShipStationError
from .models import ShipStationAccount, Store
from .serializers import (
    AssignStoreSerializer,
    RateRequestSerializer,
    ShipStationAccountSerializer,
    StoreSerializer,
)
from .tasks import sync_stores_for_account


# ── ShipStation Accounts (print center manages) ───────────────────────────────

class AccountListCreateView(APIView):
    permission_classes = (IsPrintCenter,)

    def get(self, request):
        qs = ShipStationAccount.objects.filter(
            print_center=request.user.print_center_profile
        ).order_by('-created_at')
        return Response(ShipStationAccountSerializer(qs, many=True).data)

    def post(self, request):
        serializer = ShipStationAccountSerializer(
            data=request.data,
            context={'print_center': request.user.print_center_profile},
        )
        serializer.is_valid(raise_exception=True)

        # Verify credentials before saving
        account = ShipStationAccount(
            print_center=request.user.print_center_profile,
            label=serializer.validated_data['label'],
            api_version=serializer.validated_data['api_version'],
            api_key=serializer.validated_data['api_key'],
            api_secret=serializer.validated_data.get('api_secret', ''),
        )
        if not ShipStationClient(account).verify():
            return Response(
                {'detail': 'Could not connect to ShipStation — check your API credentials'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        saved = serializer.save()
        # Trigger async store sync
        sync_stores_for_account.delay(saved.pk)
        return Response(ShipStationAccountSerializer(saved).data, status=status.HTTP_201_CREATED)


class AccountDetailView(APIView):
    permission_classes = (IsPrintCenter,)

    def _get(self, request, pk):
        try:
            return ShipStationAccount.objects.get(pk=pk, print_center=request.user.print_center_profile)
        except ShipStationAccount.DoesNotExist:
            raise NotFound()

    def get(self, request, pk):
        return Response(ShipStationAccountSerializer(self._get(request, pk)).data)

    def delete(self, request, pk):
        self._get(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def patch(self, request, pk):
        account = self._get(request, pk)
        if 'is_active' in request.data:
            account.is_active = request.data['is_active']
            account.save(update_fields=['is_active'])
        return Response(ShipStationAccountSerializer(account).data)


class AccountSyncView(APIView):
    permission_classes = (IsPrintCenter,)

    def post(self, request, pk):
        try:
            account = ShipStationAccount.objects.get(pk=pk, print_center=request.user.print_center_profile)
        except ShipStationAccount.DoesNotExist:
            raise NotFound()
        sync_stores_for_account.delay(account.pk)
        return Response({'detail': 'Store sync started'})


# ── Stores (print center manages assignments) ─────────────────────────────────

class StoreListView(generics.ListAPIView):
    permission_classes = (IsPrintCenter,)
    serializer_class = StoreSerializer

    def get_queryset(self):
        return Store.objects.filter(
            shipstation_account__print_center=self.request.user.print_center_profile
        ).select_related('shipstation_account').prefetch_related('assigned_sub_users').order_by('name')


class StoreAssignView(APIView):
    permission_classes = (IsPrintCenter,)

    def post(self, request, pk):
        try:
            store = Store.objects.get(
                pk=pk,
                shipstation_account__print_center=request.user.print_center_profile,
            )
        except Store.DoesNotExist:
            raise NotFound()

        serializer = AssignStoreSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        sub_user_ids = serializer.validated_data['sub_user_ids']
        valid_sub_users = SubUserProfile.objects.filter(
            pk__in=sub_user_ids,
            print_center=request.user.print_center_profile,
        )
        store.assigned_sub_users.set(valid_sub_users)
        return Response(StoreSerializer(store).data)


# ── Rates (sub-user fetches shipping options for an order) ────────────────────

class RatesView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        serializer = RateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Determine which ShipStation account to use (via sub-user's assigned stores)
        sub_profile = getattr(request.user, 'sub_user_profile', None)
        if not sub_profile:
            return Response({'detail': 'No sub user profile'}, status=400)

        store = sub_profile.assigned_stores.filter(is_active=True).first()
        if not store:
            return Response({'detail': 'No active store assigned'}, status=400)

        account = store.shipstation_account
        client = ShipStationClient(account)

        from_postal = account.print_center.shipstation_accounts.first()
        try:
            rates = client.get_rates(
                from_postal='75048',  # TODO: store on PrintCenterProfile
                to_postal=data['ship_to_postal_code'],
                to_country=data['ship_to_country'],
                weight_oz=data['weight_oz'],
                carrier_code=data.get('carrier_code') or None,
            )
        except ShipStationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        # Apply sub-user shipping markup
        markup_type = sub_profile.shipping_markup_type
        markup_value = float(sub_profile.shipping_markup_value)
        result = []
        for r in rates:
            base = float(r.get('shipmentCost', 0)) + float(r.get('otherCost', 0))
            if markup_type == 'percent':
                displayed = base * (1 + markup_value / 100)
            else:
                displayed = base + markup_value
            result.append({
                'carrier_code': r.get('carrierCode'),
                'service_code': r.get('serviceCode'),
                'service_name': r.get('serviceName'),
                'base_rate': round(base, 2),
                'displayed_rate': round(displayed, 2),
            })

        return Response({'rates': result})


# ── ShipStation Webhook ───────────────────────────────────────────────────────

class ShipStationWebhookView(APIView):
    permission_classes = (AllowAny,)

    def post(self, request, account_id):
        try:
            account = ShipStationAccount.objects.get(pk=account_id, is_active=True)
        except ShipStationAccount.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        resource_type = request.data.get('resource_type', '')
        resource_url = request.data.get('resource_url', '')

        if resource_type == 'ORDER_NOTIFY' or 'orders' in resource_url:
            from .tasks import sync_orders_for_store
            for store in account.stores.filter(is_active=True):
                sync_orders_for_store.delay(store.pk)

        return Response({'detail': 'ok'})
