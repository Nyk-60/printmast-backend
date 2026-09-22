from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import PrintCenterProfile, SubUserProfile
from apps.accounts.permissions import IsPrintCenter

from .byelabel_client import ByeLabelClient
from .client import ShipStationClient
from .models import (
    FixedShippingRate,
    LabelProviderAccount,
    ShipStationAccount,
    ShippingServiceDefault,
    ShippingServiceOption,
    Store,
)
from .serializers import (
    AssignStoreSerializer,
    FixedShippingRateSerializer,
    LabelProviderAccountSerializer,
    RateRequestSerializer,
    ShippingServiceDefaultSerializer,
    ShippingServiceOptionSerializer,
    ShipStationAccountSerializer,
    StoreSerializer,
)
from .tasks import register_webhooks_for_account, sync_stores_for_account


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
        ok, err = ShipStationClient(account).verify()
        if not ok:
            return Response(
                {'detail': f'Could not connect to ShipStation: {err}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        saved = serializer.save()
        try:
            sync_stores_for_account.delay(saved.pk)
            register_webhooks_for_account.delay(saved.pk)
        except Exception:
            sync_stores_for_account(saved.pk)
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
        update_fields = []

        if 'is_active' in request.data:
            account.is_active = bool(request.data['is_active'])
            update_fields.append('is_active')

        if 'label' in request.data:
            account.label = request.data['label']
            update_fields.append('label')

        new_key    = request.data.get('api_key', '').strip()
        new_secret = request.data.get('api_secret', '').strip()
        if new_key:
            test = ShipStationAccount(
                print_center=account.print_center,
                label=account.label,
                api_version=account.api_version,
                api_key=new_key,
                api_secret=new_secret or account.api_secret,
            )
            ok, err = ShipStationClient(test).verify()
            if not ok:
                return Response(
                    {'detail': f'Could not connect to ShipStation: {err}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            account.api_key = new_key
            update_fields.append('api_key')
            if new_secret:
                account.api_secret = new_secret
                update_fields.append('api_secret')

        if update_fields:
            account.save(update_fields=update_fields)
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


class AccountWebhookRegisterView(APIView):
    """Register ORDER_NOTIFY + SHIP_NOTIFY webhooks for an account."""
    permission_classes = (IsPrintCenter,)

    def post(self, request, pk):
        try:
            account = ShipStationAccount.objects.get(pk=pk, print_center=request.user.print_center_profile)
        except ShipStationAccount.DoesNotExist:
            raise NotFound()
        try:
            register_webhooks_for_account.delay(account.pk)
        except Exception:
            register_webhooks_for_account(account.pk)
        return Response({'detail': 'Webhook registration started'})


# ── Stores (print center manages assignments) ─────────────────────────────────

class StoreListView(generics.ListAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = StoreSerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_sub_user:
            return (
                Store.objects.filter(assigned_sub_users=user.sub_user_profile)
                .select_related('shipstation_account')
                .order_by('name')
            )
        return Store.objects.filter(
            shipstation_account__print_center=user.print_center_profile
        ).select_related('shipstation_account').prefetch_related('assigned_sub_users').order_by('name')


class StoreSyncView(APIView):
    """Manually trigger an order sync for a specific store."""
    permission_classes = (IsPrintCenter,)

    def post(self, request, pk):
        try:
            store = Store.objects.get(
                pk=pk,
                shipstation_account__print_center=request.user.print_center_profile,
            )
        except Store.DoesNotExist:
            raise NotFound()
        from .tasks import sync_orders_for_store
        try:
            sync_orders_for_store.delay(store.pk)
        except Exception:
            sync_orders_for_store(store.pk)
        return Response({'detail': 'Order sync started'})


class StoreSyncShipFromView(APIView):
    """Manually re-pull this store's ship-from address from its ShipStation warehouse."""
    permission_classes = (IsPrintCenter,)

    def post(self, request, pk):
        try:
            store = Store.objects.get(
                pk=pk,
                shipstation_account__print_center=request.user.print_center_profile,
            )
        except Store.DoesNotExist:
            raise NotFound()

        from .tasks import sync_store_ship_from_address

        if not store.shipstation_warehouse_id:
            # Not discovered from an order yet — try once now via ShipStation's
            # warehouse list, matching by name as a best-effort first link.
            warehouses = ShipStationClient(store.shipstation_account).get_warehouses()
            match = next((w for w in warehouses if w.get('warehouseName') == store.name), None)
            if match:
                store.shipstation_warehouse_id = str(match.get('warehouseId'))
                store.save(update_fields=['shipstation_warehouse_id'])

        ok = sync_store_ship_from_address(store)
        if not ok:
            return Response(
                {'detail': 'No matching ShipStation warehouse found for this store yet. '
                           'Configure a Ship From location for it in ShipStation, or sync orders first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        store.refresh_from_db()
        return Response(StoreSerializer(store).data)


class MyStoresSyncView(APIView):
    """Sub-user triggers a sync for all their assigned stores."""
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        from datetime import timedelta
        import threading
        from django.utils import timezone
        from .tasks import sync_orders_for_store

        user = request.user
        if user.is_sub_user:
            stores = Store.objects.filter(
                assigned_sub_users=user.sub_user_profile, is_active=True
            )
        elif user.is_print_center:
            stores = Store.objects.filter(
                shipstation_account__print_center=user.print_center_profile, is_active=True
            )
        else:
            return Response({'detail': 'No stores to sync'}, status=400)

        store_list = list(stores)
        if not store_list:
            return Response({'detail': 'No stores assigned'}, status=400)

        # 7-day window for status refresh (shipped/cancelled since last sync).
        # This uses update_only=True so no new records are created from old data.
        status_window = (timezone.now() - timedelta(days=7)).isoformat()

        def _run(pk):
            from django.db import close_old_connections
            close_old_connections()
            try:
                # 1. Full import: pull awaiting_shipment orders (creates new records)
                sync_orders_for_store(pk)
            except Exception:
                pass
            try:
                # 2. Status refresh: catch shipped/cancelled transitions on existing orders
                sync_orders_for_store(pk, modified_after_iso=status_window, update_only=True)
            except Exception:
                pass

        for store in store_list:
            threading.Thread(target=_run, args=(store.pk,), daemon=True).start()

        return Response({'detail': f'Sync started for {len(store_list)} store(s)'})


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


# ── Store label settings (sub-user updates sender name for their store) ──────

class StoreUpdateView(APIView):
    """Sub-user sets the sender name/company shown on labels for one of their stores."""
    permission_classes = (IsAuthenticated,)

    def patch(self, request, pk):
        user = request.user
        if user.is_sub_user:
            try:
                store = Store.objects.get(pk=pk, assigned_sub_users=user.sub_user_profile)
            except Store.DoesNotExist:
                raise NotFound()
        elif user.is_print_center:
            try:
                store = Store.objects.get(
                    pk=pk, shipstation_account__print_center=user.print_center_profile)
            except Store.DoesNotExist:
                raise NotFound()
        else:
            raise NotFound()

        for field in ('ship_from_name', 'ship_from_company'):
            if field in request.data:
                setattr(store, field, request.data[field] or '')
        store.save(update_fields=['ship_from_name', 'ship_from_company'])
        return Response(StoreSerializer(store).data)


# ── Label provider accounts (print center enables/disables label-purchase sources) ──

class LabelProviderAccountListCreateView(APIView):
    permission_classes = (IsPrintCenter,)

    def get(self, request):
        qs = LabelProviderAccount.objects.filter(
            print_center=request.user.print_center_profile
        ).select_related('shipstation_account')
        return Response(LabelProviderAccountSerializer(qs, many=True).data)

    def post(self, request):
        serializer = LabelProviderAccountSerializer(
            data=request.data, context={'print_center': request.user.print_center_profile},
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data['provider'] == LabelProviderAccount.Provider.BYELABEL:
            account = LabelProviderAccount(print_center=request.user.print_center_profile, **data)
            ok, err = ByeLabelClient(account).verify()
            if not ok:
                return Response({'detail': f'Could not connect to ByeLabel: {err}'}, status=status.HTTP_400_BAD_REQUEST)

        saved = serializer.save()
        return Response(LabelProviderAccountSerializer(saved).data, status=status.HTTP_201_CREATED)


class LabelProviderAccountDetailView(APIView):
    permission_classes = (IsPrintCenter,)

    def _get(self, request, pk):
        try:
            return LabelProviderAccount.objects.get(pk=pk, print_center=request.user.print_center_profile)
        except LabelProviderAccount.DoesNotExist:
            raise NotFound()

    def patch(self, request, pk):
        account = self._get(request, pk)
        serializer = LabelProviderAccountSerializer(
            account, data=request.data, partial=True,
            context={'print_center': request.user.print_center_profile},
        )
        serializer.is_valid(raise_exception=True)

        if (account.provider == LabelProviderAccount.Provider.BYELABEL
                and any(f in serializer.validated_data for f in
                        ('byelabel_client_id', 'byelabel_client_secret', 'byelabel_workspace_id'))):
            test = LabelProviderAccount(
                print_center=account.print_center,
                byelabel_client_id=serializer.validated_data.get('byelabel_client_id', account.byelabel_client_id),
                byelabel_client_secret=serializer.validated_data.get('byelabel_client_secret') or account.byelabel_client_secret,
                byelabel_workspace_id=serializer.validated_data.get('byelabel_workspace_id', account.byelabel_workspace_id),
            )
            ok, err = ByeLabelClient(test).verify()
            if not ok:
                return Response({'detail': f'Could not connect to ByeLabel: {err}'}, status=status.HTTP_400_BAD_REQUEST)

        serializer.save()
        return Response(LabelProviderAccountSerializer(account).data)

    def delete(self, request, pk):
        self._get(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Shipping service options (print center configures per category) ──────────

class ServiceOptionListCreateView(APIView):
    """Print center manages which carrier/service options it offers per category."""
    permission_classes = (IsPrintCenter,)

    def get(self, request):
        qs = ShippingServiceOption.objects.filter(
            print_center=request.user.print_center_profile
        )
        return Response(ShippingServiceOptionSerializer(qs, many=True).data)

    def post(self, request):
        serializer = ShippingServiceOptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        obj = ShippingServiceOption.objects.create(
            print_center=request.user.print_center_profile,
            **serializer.validated_data,
        )
        return Response(ShippingServiceOptionSerializer(obj).data, status=status.HTTP_201_CREATED)


class ServiceOptionDetailView(APIView):
    permission_classes = (IsPrintCenter,)

    def _get(self, request, pk):
        try:
            return ShippingServiceOption.objects.get(
                pk=pk, print_center=request.user.print_center_profile
            )
        except ShippingServiceOption.DoesNotExist:
            raise NotFound()

    def patch(self, request, pk):
        obj = self._get(request, pk)
        serializer = ShippingServiceOptionSerializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(ShippingServiceOptionSerializer(obj).data)

    def delete(self, request, pk):
        self._get(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Fixed shipping rates (flat price table, used when pricing_mode == 'fixed') ─

class FixedShippingRateListCreateView(APIView):
    permission_classes = (IsPrintCenter,)

    def get(self, request):
        qs = FixedShippingRate.objects.filter(print_center=request.user.print_center_profile)
        return Response(FixedShippingRateSerializer(qs, many=True).data)

    def post(self, request):
        serializer = FixedShippingRateSerializer(
            data=request.data, context={'print_center': request.user.print_center_profile},
        )
        serializer.is_valid(raise_exception=True)
        obj = serializer.save()
        return Response(FixedShippingRateSerializer(obj).data, status=status.HTTP_201_CREATED)


class FixedShippingRateDetailView(APIView):
    permission_classes = (IsPrintCenter,)

    def _get(self, request, pk):
        try:
            return FixedShippingRate.objects.get(pk=pk, print_center=request.user.print_center_profile)
        except FixedShippingRate.DoesNotExist:
            raise NotFound()

    def patch(self, request, pk):
        obj = self._get(request, pk)
        serializer = FixedShippingRateSerializer(
            obj, data=request.data, partial=True,
            context={'print_center': request.user.print_center_profile},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(FixedShippingRateSerializer(obj).data)

    def delete(self, request, pk):
        self._get(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Shipping service defaults (sub-user picks default per category) ──────────

class MyServiceOptionsView(APIView):
    """Sub-user: list their print center's active service options, for picking defaults.

    ShipStation-only: this picks the displayed rate shown ahead of send-to-print.
    Which provider actually buys the label is a separate, later comparison.
    """
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        sub_profile = getattr(request.user, 'sub_user_profile', None)
        if not sub_profile:
            return Response({'detail': 'No sub user profile'}, status=400)
        qs = ShippingServiceOption.objects.filter(
            print_center=sub_profile.print_center, is_active=True, provider='shipstation',
        )
        return Response(ShippingServiceOptionSerializer(qs, many=True).data)


class MyServiceDefaultsView(APIView):
    """Sub-user: get/set their default service option per category."""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        sub_profile = getattr(request.user, 'sub_user_profile', None)
        if not sub_profile:
            return Response({'detail': 'No sub user profile'}, status=400)
        defaults = {
            d.category: d for d in
            ShippingServiceDefault.objects.filter(sub_user=sub_profile).select_related('service_option')
        }
        result = []
        for cat, _ in ShippingServiceOption.Category.choices:
            d = defaults.get(cat)
            if d:
                result.append(ShippingServiceDefaultSerializer(d).data)
            else:
                result.append({'category': cat, 'service_option': None, 'service_option_detail': None})
        return Response(result)

    def put(self, request):
        sub_profile = getattr(request.user, 'sub_user_profile', None)
        if not sub_profile:
            return Response({'detail': 'No sub user profile'}, status=400)

        category = request.data.get('category')
        if category not in dict(ShippingServiceOption.Category.choices):
            return Response({'detail': 'Invalid category'}, status=400)

        serializer = ShippingServiceDefaultSerializer(
            data=request.data, context={'sub_user': sub_profile}
        )
        serializer.is_valid(raise_exception=True)
        obj, _ = ShippingServiceDefault.objects.update_or_create(
            sub_user=sub_profile, category=category,
            defaults={'service_option': serializer.validated_data.get('service_option')},
        )
        return Response(ShippingServiceDefaultSerializer(obj).data)


# ── Rates (sub-user fetches shipping options for an order) ────────────────────

class RatesView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        serializer = RateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Determine which store/ShipStation account to use — prefer the order's
        # actual store (each store has its own ship-from address), falling back
        # to the sub-user's first assigned store only when no order is given.
        sub_profile = getattr(request.user, 'sub_user_profile', None)
        if not sub_profile:
            return Response({'detail': 'No sub user profile'}, status=400)

        dimensions = {'length': 12, 'width': 10, 'height': 2, 'units': 'inches'}
        order = None
        order_id = data.get('order_id')
        if order_id:
            from apps.orders.models import Order
            from .utils import calculate_order_dimensions
            try:
                order = Order.objects.select_related('store__shipstation_account').prefetch_related(
                    'items__pc_variant__dimension_rules'
                ).get(id=order_id, sub_user=sub_profile)
                dimensions = calculate_order_dimensions(order)
            except Order.DoesNotExist:
                pass

        store = order.store if order else sub_profile.assigned_stores.filter(is_active=True).first()
        if not store:
            return Response({'detail': 'No active store assigned'}, status=400)

        account = store.shipstation_account

        if not store.ship_from_configured:
            return Response(
                {'detail': f'"{store.name}" has no ship-from address yet. Sync it from '
                           'ShipStation in the ShipStation Integration page before quoting rates.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        pc = account.print_center

        from .utils import categorize_requested_service, real_service_key
        category = categorize_requested_service(order.requested_service if order else '')

        if pc.shipping_pricing_mode == PrintCenterProfile.ShippingPricingMode.FIXED:
            from .utils import get_fixed_shipping_price
            price = get_fixed_shipping_price(pc, category, data['weight_oz'])
            if price is None:
                return Response(
                    {'detail': f'No fixed shipping price configured for "{category}" at this weight.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            markup_type = sub_profile.shipping_markup_type
            markup_value = float(sub_profile.shipping_markup_value)
            base = float(price)
            displayed = base * (1 + markup_value / 100) if markup_type == 'percent' else base + markup_value
            return Response({'rates': [{
                'carrier_code': 'fixed',
                'service_code': category,
                'service_name': f'{category.title()} Shipping',
                'base_rate': round(base, 2),
                'displayed_rate': round(displayed, 2),
                'is_default': True,
            }]})

        # Compare every active provider (ShipStation, ByeLabel, ...) the print center has
        # configured for this category — not just ShipStation — so the price shown to the
        # sub-user before send-to-print already reflects whichever is actually cheapest.
        from .services import shop_all_rates

        quotes = shop_all_rates(
            pc, store, category,
            to_postal=data['ship_to_postal_code'], to_country=data['ship_to_country'],
            weight_oz=data['weight_oz'], dimensions=dimensions,
            carrier_code=data.get('carrier_code') or None,
            label=f"order {order_id or 'n/a'} / sub-user {sub_profile.id}",
        )
        if not quotes:
            return Response(
                {'detail': 'No rate could be quoted for this address/weight from any configured provider.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Collapse to one line per REAL-WORLD service: ShipStation's stamps_com account
        # and ByeLabel's own usps account can both quote "USPS Ground Advantage" at
        # different prices — the sub-user should see that once (whichever is cheaper),
        # not two confusingly near-identical, differently priced lines. Different real
        # carriers/services (USPS vs UPS) are never merged (see real_service_key).
        best_by_key = {}
        for q in quotes:
            key = q['real_key']
            if key not in best_by_key or q['cost'] < best_by_key[key]['cost']:
                best_by_key[key] = q

        # Apply sub-user shipping markup
        markup_type = sub_profile.shipping_markup_type
        markup_value = float(sub_profile.shipping_markup_value)

        default = ShippingServiceDefault.objects.filter(
            sub_user=sub_profile, category=category, service_option__isnull=False,
        ).select_related('service_option').first()
        default_key = (
            real_service_key(default.service_option.carrier_code, default.service_option.service_code)
            if default else None
        )
        result = []
        for q in best_by_key.values():
            base = q['cost']
            if markup_type == 'percent':
                displayed = base * (1 + markup_value / 100)
            else:
                displayed = base + markup_value
            result.append({
                'carrier_code': q['carrier_code'],
                'service_code': q['service_code'],
                'service_name': q['service_name'],
                'provider': q['provider'],
                'base_rate': round(base, 2),
                'displayed_rate': round(displayed, 2),
                'is_default': q['real_key'] == default_key,
            })

        result.sort(key=lambda r: r['displayed_rate'])

        return Response({'rates': result})


# ── ShipStation Webhook ───────────────────────────────────────────────────────

class ShipStationWebhookView(APIView):
    """
    ShipStation calls this endpoint when orders change (ORDER_NOTIFY, SHIP_NOTIFY).
    Register URL in ShipStation → Settings → Webhooks:
      https://<your-domain>/shipping/webhook/<account_id>/
    """
    permission_classes = (AllowAny,)

    def post(self, request, account_id):
        try:
            account = ShipStationAccount.objects.get(pk=account_id, is_active=True)
        except ShipStationAccount.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        resource_type = (request.data.get('resource_type') or '').upper()
        resource_url  = request.data.get('resource_url') or ''

        if (resource_type in ('ORDER_NOTIFY', 'SHIP_NOTIFY')
                or 'orders' in resource_url or 'shipments' in resource_url):
            from .tasks import sync_single_order_from_webhook
            try:
                sync_single_order_from_webhook.delay(account.pk, resource_url, resource_type)
            except Exception:
                sync_single_order_from_webhook(account.pk, resource_url, resource_type)

        return Response({'detail': 'ok'})
