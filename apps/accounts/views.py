import io

import pytz
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone as tz
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    BalanceTransaction,
    ImpersonationLog,
    PrintCenterInvoice,
    PrintCenterPricingTier,
    PrintCenterProfile,
    SubUserProfile,
    User,
)
from .permissions import IsPrintCenter, IsSuperAdmin
from .serializers import (
    AddBalanceSerializer,
    BalanceTransactionSerializer,
    CreatePrintCenterSerializer,
    LoginSerializer,
    PrintCenterInvoiceSerializer,
    PrintCenterPricingTierSerializer,
    PrintCenterProfileSerializer,
    RegisterPrintCenterSerializer,
    ShippingAutoRuleSerializer,
    SubUserCreateSerializer,
    SubUserProfileSerializer,
    UpdatePrintCenterSerializer,
    UserSerializer,
)


# ── Auth ──────────────────────────────────────────────────────────────────────

class RegisterView(APIView):
    """Self-registration is disabled — print centers are created by super admin."""
    permission_classes = (AllowAny,)

    def post(self, request):
        raise PermissionDenied('Self-registration is disabled. Contact your administrator.')


class LoginView(APIView):
    permission_classes = (AllowAny,)

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data)


class LogoutView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        data = UserSerializer(request.user).data
        if request.user.is_print_center:
            try:
                data['profile'] = PrintCenterProfileSerializer(
                    request.user.print_center_profile,
                    context={'request': request},
                ).data
            except PrintCenterProfile.DoesNotExist:
                pass
        elif request.user.is_sub_user:
            try:
                data['profile'] = SubUserProfileSerializer(
                    request.user.sub_user_profile,
                    context={'request': request},
                ).data
            except SubUserProfile.DoesNotExist:
                pass
        return Response(data)

    def patch(self, request):
        user = request.user
        if 'full_name' in request.data:
            user.full_name = request.data['full_name']
            user.save(update_fields=['full_name'])
        if 'password' in request.data:
            user.set_password(request.data['password'])
            user.save(update_fields=['password'])

        if user.is_sub_user:
            PROFILE_BOOL_FIELDS = ('auto_product_matching', 'auto_design_matching', 'auto_shipping_rules')
            profile = user.sub_user_profile
            update_fields = []
            for field in PROFILE_BOOL_FIELDS:
                if field in request.data:
                    setattr(profile, field, bool(request.data[field]))
                    update_fields.append(field)
            if update_fields:
                profile.save(update_fields=update_fields)

        return Response(UserSerializer(user).data)


# ── Print Center — Shipping Settings (ship-from address) ─────────────────────

class PCShippingSettingsView(APIView):
    permission_classes = (IsPrintCenter,)

    SHIP_FROM_FIELDS = [
        'ship_from_name', 'ship_from_phone',
        'ship_from_street1', 'ship_from_street2',
        'ship_from_city', 'ship_from_state',
        'ship_from_postal_code', 'ship_from_country',
    ]

    def get(self, request):
        pc = request.user.print_center_profile
        return Response(PrintCenterProfileSerializer(pc, context={'request': request}).data)

    def patch(self, request):
        pc = request.user.print_center_profile
        update_fields = []
        for field in self.SHIP_FROM_FIELDS:
            if field in request.data:
                setattr(pc, field, request.data[field] or '')
                update_fields.append(field)
        if update_fields:
            pc.save(update_fields=update_fields)
        return Response(PrintCenterProfileSerializer(pc, context={'request': request}).data)


# ── SuperAdmin — Dashboard Stats ──────────────────────────────────────────────

class SuperAdminDashboardStatsView(APIView):
    permission_classes = (IsSuperAdmin,)

    def get(self, request):
        from apps.orders.models import Order, OrderItem
        from apps.shipping.models import Store

        active_pcs = PrintCenterProfile.objects.filter(status=PrintCenterProfile.Status.ACTIVE).count()
        active_clients = SubUserProfile.objects.filter(is_active=True).count()
        active_shops = Store.objects.filter(assigned_sub_users__isnull=False).distinct().count()

        processed_orders = Order.objects.filter(labeled_at__isnull=False).count()
        processed_items = (
            OrderItem.objects.filter(order__labeled_at__isnull=False)
            .aggregate(total=Sum('quantity'))['total'] or 0
        )

        return Response({
            'active_print_centers': active_pcs,
            'active_clients': active_clients,
            'active_shops': active_shops,
            'processed_orders': processed_orders,
            'processed_items': processed_items,
        })


# ── SuperAdmin — Print Center CRUD ────────────────────────────────────────────

def _annotated_pc_queryset():
    from apps.orders.models import Order
    return (
        PrintCenterProfile.objects
        .select_related('user')
        .annotate(
            sub_user_count_ann=Count('sub_users', distinct=True),
            active_shop_count=Count('sub_users__assigned_stores', distinct=True),
            awaiting_order_count=Count(
                'sub_users__orders',
                filter=Q(sub_users__orders__status__in=[
                    'new', 'matching', 'ready', 'sent_to_print', 'in_production'
                ]),
                distinct=True,
            ),
            completed_order_count=Count(
                'sub_users__orders',
                filter=Q(sub_users__orders__labeled_at__isnull=False),
                distinct=True,
            ),
        )
        .order_by('-created_at')
    )


class PrintCenterListView(generics.ListAPIView):
    permission_classes = (IsSuperAdmin,)
    serializer_class = PrintCenterProfileSerializer

    def get_queryset(self):
        qs = _annotated_pc_queryset()
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(company_name__icontains=search) | Q(user__email__icontains=search)
            )
        return qs

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['request'] = self.request
        return ctx


class PrintCenterCreateView(APIView):
    permission_classes = (IsSuperAdmin,)
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request):
        serializer = CreatePrintCenterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save()
        return Response(
            PrintCenterProfileSerializer(profile, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class PrintCenterDetailView(APIView):
    permission_classes = (IsSuperAdmin,)
    parser_classes = (MultiPartParser, FormParser)

    def _get_object(self, pk):
        try:
            return _annotated_pc_queryset().get(pk=pk)
        except PrintCenterProfile.DoesNotExist:
            raise NotFound()

    def get(self, request, pk):
        return Response(
            PrintCenterProfileSerializer(self._get_object(pk), context={'request': request}).data
        )

    def patch(self, request, pk):
        pc = self._get_object(pk)
        serializer = UpdatePrintCenterSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        update_fields = []
        if 'status' in data:
            pc.status = data['status']
            pc.user.is_active = data['status'] == PrintCenterProfile.Status.ACTIVE
            pc.user.save(update_fields=['is_active'])
            update_fields.append('status')
        if 'company_name' in data:
            pc.company_name = data['company_name']
            update_fields.append('company_name')
        if 'timezone' in data:
            pc.timezone = data['timezone']
            update_fields.append('timezone')
        if 'monthly_fee' in data:
            pc.monthly_fee = data['monthly_fee']
            update_fields.append('monthly_fee')
        if 'max_sub_users' in data:
            pc.max_sub_users = data['max_sub_users']
            update_fields.append('max_sub_users')
        if 'logo' in data and data['logo'] is not None:
            pc.logo = data['logo']
            update_fields.append('logo')
        if update_fields:
            pc.save(update_fields=update_fields)

        if 'email' in data:
            pc.user.email = data['email']
            pc.user.save(update_fields=['email'])
        if 'password' in data:
            pc.user.set_password(data['password'])
            pc.user.save(update_fields=['password'])

        return Response(
            PrintCenterProfileSerializer(self._get_object(pk), context={'request': request}).data
        )


# ── SuperAdmin — Impersonation ────────────────────────────────────────────────

class PrintCenterImpersonateView(APIView):
    permission_classes = (IsSuperAdmin,)

    def post(self, request, pk):
        try:
            pc = PrintCenterProfile.objects.select_related('user').get(pk=pk)
        except PrintCenterProfile.DoesNotExist:
            raise NotFound()

        ImpersonationLog.objects.create(admin=request.user, print_center=pc)

        refresh = RefreshToken.for_user(pc.user)
        return Response({
            'access': str(refresh.access_token),
            'print_center': {
                'id': pc.id,
                'company_name': pc.company_name,
                'email': pc.user.email,
            },
        })


# ── SuperAdmin — Print Center Analytics ───────────────────────────────────────

class PrintCenterAnalyticsView(APIView):
    permission_classes = (IsSuperAdmin,)

    def get(self, request, pk):
        try:
            pc = PrintCenterProfile.objects.get(pk=pk)
        except PrintCenterProfile.DoesNotExist:
            raise NotFound()

        start_str = request.query_params.get('start')
        end_str = request.query_params.get('end')
        fmt = request.query_params.get('format', 'json')

        try:
            pc_tz = pytz.timezone(pc.timezone)
        except Exception:
            pc_tz = pytz.utc

        from apps.orders.models import Order, OrderItem

        qs = Order.objects.filter(
            sub_user__print_center=pc,
            labeled_at__isnull=False,
        ).select_related('sub_user')

        if start_str:
            try:
                from datetime import datetime
                start_local = datetime.strptime(start_str, '%Y-%m-%d')
                start_utc = pc_tz.localize(start_local).astimezone(pytz.utc)
                qs = qs.filter(labeled_at__gte=start_utc)
            except ValueError:
                pass
        if end_str:
            try:
                from datetime import datetime, timedelta
                end_local = datetime.strptime(end_str, '%Y-%m-%d') + timedelta(days=1)
                end_utc = pc_tz.localize(end_local).astimezone(pytz.utc)
                qs = qs.filter(labeled_at__lt=end_utc)
            except ValueError:
                pass

        order_ids = list(qs.values_list('id', flat=True))
        order_count = len(order_ids)

        items = (
            OrderItem.objects.filter(order_id__in=order_ids)
            .select_related('pc_variant__pc_product', 'pc_variant')
        )

        item_count = sum(i.quantity for i in items)

        # Build product breakdown
        product_map = {}
        for item in items:
            variant = item.pc_variant
            if variant:
                pname = variant.pc_product.name
                psku = variant.pc_product.sku if hasattr(variant.pc_product, 'sku') else ''
                color = variant.color or ''
                size = variant.size or ''
            else:
                pname = item.raw_product_name or 'Unknown'
                psku = item.marketplace_sku or ''
                color = ''
                size = ''

            if pname not in product_map:
                product_map[pname] = {'name': pname, 'sku': psku, 'count': 0, 'variants': {}}
            product_map[pname]['count'] += item.quantity

            vkey = f'{color}|{size}'
            if vkey not in product_map[pname]['variants']:
                product_map[pname]['variants'][vkey] = {'color': color, 'size': size, 'count': 0}
            product_map[pname]['variants'][vkey]['count'] += item.quantity

        products = []
        for pdata in sorted(product_map.values(), key=lambda x: -x['count']):
            products.append({
                'name': pdata['name'],
                'sku': pdata['sku'],
                'count': pdata['count'],
                'variants': sorted(pdata['variants'].values(), key=lambda x: (-x['count'], x['color'], x['size'])),
            })

        result = {
            'print_center': pc.company_name,
            'timezone': pc.timezone,
            'order_count': order_count,
            'item_count': item_count,
            'products': products,
        }

        if fmt == 'xlsx':
            return self._excel_response(result, pc.company_name)
        return Response(result)

    def _excel_response(self, data, pc_name):
        import openpyxl
        from django.http import HttpResponse

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Summary'

        ws.append(['Print Center', data['print_center']])
        ws.append(['Timezone', data['timezone']])
        ws.append(['Total Orders', data['order_count']])
        ws.append(['Total Items', data['item_count']])
        ws.append([])
        ws.append(['Product', 'SKU', 'Total Items', 'Color', 'Size', 'Count'])

        for p in data['products']:
            first = True
            for v in p['variants']:
                if first:
                    ws.append([p['name'], p['sku'], p['count'], v['color'], v['size'], v['count']])
                    first = False
                else:
                    ws.append(['', '', '', v['color'], v['size'], v['count']])
            if not p['variants']:
                ws.append([p['name'], p['sku'], p['count'], '', '', ''])

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        resp = HttpResponse(
            buf.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        resp['Content-Disposition'] = f'attachment; filename="{pc_name}_analytics.xlsx"'
        return resp


# ── SuperAdmin — Pricing Tiers ────────────────────────────────────────────────

class PrintCenterPricingTierView(APIView):
    permission_classes = (IsSuperAdmin,)

    def _get_pc(self, pk):
        try:
            return PrintCenterProfile.objects.get(pk=pk)
        except PrintCenterProfile.DoesNotExist:
            raise NotFound()

    def get(self, request, pk):
        pc = self._get_pc(pk)
        tiers = pc.pricing_tiers.all()
        return Response(PrintCenterPricingTierSerializer(tiers, many=True).data)

    def post(self, request, pk):
        pc = self._get_pc(pk)
        serializer = PrintCenterPricingTierSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tier = serializer.save(print_center=pc)
        return Response(PrintCenterPricingTierSerializer(tier).data, status=status.HTTP_201_CREATED)


class PrintCenterPricingTierDetailView(APIView):
    permission_classes = (IsSuperAdmin,)

    def _get_tier(self, pk, tier_pk):
        try:
            return PrintCenterPricingTier.objects.get(pk=tier_pk, print_center_id=pk)
        except PrintCenterPricingTier.DoesNotExist:
            raise NotFound()

    def patch(self, request, pk, tier_pk):
        tier = self._get_tier(pk, tier_pk)
        serializer = PrintCenterPricingTierSerializer(tier, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk, tier_pk):
        tier = self._get_tier(pk, tier_pk)
        tier.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── SuperAdmin — Invoices ─────────────────────────────────────────────────────

class InvoiceListView(generics.ListAPIView):
    permission_classes = (IsSuperAdmin,)
    serializer_class = PrintCenterInvoiceSerializer

    def get_queryset(self):
        qs = PrintCenterInvoice.objects.select_related('print_center')
        pc_id = self.request.query_params.get('print_center')
        inv_status = self.request.query_params.get('status')
        if pc_id:
            qs = qs.filter(print_center_id=pc_id)
        if inv_status:
            qs = qs.filter(status=inv_status)
        return qs


class InvoiceDetailView(APIView):
    permission_classes = (IsSuperAdmin,)

    def _get_invoice(self, invoice_pk):
        try:
            return PrintCenterInvoice.objects.select_related('print_center').get(pk=invoice_pk)
        except PrintCenterInvoice.DoesNotExist:
            raise NotFound()

    def patch(self, request, invoice_pk):
        invoice = self._get_invoice(invoice_pk)
        allowed = {k: v for k, v in request.data.items() if k in ('status', 'note', 'paid_at')}
        if 'status' in allowed and allowed['status'] == 'paid' and not invoice.paid_at:
            allowed['paid_at'] = tz.now().isoformat()
        for k, v in allowed.items():
            setattr(invoice, k, v)
        invoice.save()
        return Response(PrintCenterInvoiceSerializer(invoice).data)


# ── Print Center — Sub User Management ───────────────────────────────────────

class SubUserListCreateView(APIView):
    permission_classes = (IsPrintCenter,)

    def get(self, request):
        qs = SubUserProfile.objects.filter(
            print_center=request.user.print_center_profile
        ).select_related('user').order_by('-created_at')
        return Response(SubUserProfileSerializer(qs, many=True).data)

    def post(self, request):
        serializer = SubUserCreateSerializer(
            data=request.data,
            context={'request': request, 'print_center': request.user.print_center_profile},
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            SubUserProfileSerializer(user.sub_user_profile).data,
            status=status.HTTP_201_CREATED,
        )


class SubUserDetailView(APIView):
    permission_classes = (IsPrintCenter,)

    def _get_object(self, request, pk):
        try:
            return SubUserProfile.objects.select_related('user').get(
                pk=pk, print_center=request.user.print_center_profile
            )
        except SubUserProfile.DoesNotExist:
            raise NotFound()

    def get(self, request, pk):
        return Response(SubUserProfileSerializer(self._get_object(request, pk)).data)

    def patch(self, request, pk):
        profile = self._get_object(request, pk)
        serializer = SubUserProfileSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class SubUserToggleActiveView(APIView):
    permission_classes = (IsPrintCenter,)

    def post(self, request, pk):
        try:
            profile = SubUserProfile.objects.select_related('user').get(
                pk=pk, print_center=request.user.print_center_profile
            )
        except SubUserProfile.DoesNotExist:
            raise NotFound()
        profile.is_active = not profile.is_active
        profile.user.is_active = profile.is_active
        profile.save(update_fields=['is_active'])
        profile.user.save(update_fields=['is_active'])
        return Response({'is_active': profile.is_active})


class AddBalanceView(APIView):
    permission_classes = (IsPrintCenter,)

    @transaction.atomic
    def post(self, request, pk):
        try:
            profile = SubUserProfile.objects.select_for_update().get(
                pk=pk, print_center=request.user.print_center_profile
            )
        except SubUserProfile.DoesNotExist:
            raise NotFound()
        serializer = AddBalanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        amount = serializer.validated_data['amount']
        profile.balance += amount
        profile.save(update_fields=['balance'])
        tx = BalanceTransaction.objects.create(
            sub_user=profile,
            type=BalanceTransaction.Type.CREDIT,
            amount=amount,
            description=serializer.validated_data.get('description', ''),
            created_by=request.user,
        )
        return Response(BalanceTransactionSerializer(tx).data, status=status.HTTP_201_CREATED)


class SubUserTransactionsView(generics.ListAPIView):
    permission_classes = (IsPrintCenter,)
    serializer_class = BalanceTransactionSerializer

    def get_queryset(self):
        if not SubUserProfile.objects.filter(
            pk=self.kwargs['pk'],
            print_center=self.request.user.print_center_profile
        ).exists():
            raise NotFound()
        return BalanceTransaction.objects.filter(
            sub_user__pk=self.kwargs['pk']
        ).order_by('-created_at')


# ── Sub User — Self ───────────────────────────────────────────────────────────

class MyTransactionsView(generics.ListAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = BalanceTransactionSerializer

    def get_queryset(self):
        return BalanceTransaction.objects.filter(
            sub_user=self.request.user.sub_user_profile
        ).order_by('-created_at')


class MyShippingRulesView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        rules = request.user.sub_user_profile.shipping_rules.all()
        return Response(ShippingAutoRuleSerializer(rules, many=True).data)

    def post(self, request):
        serializer = ShippingAutoRuleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(sub_user=request.user.sub_user_profile)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class MyShippingRuleDeleteView(APIView):
    permission_classes = (IsAuthenticated,)

    def delete(self, request, rule_id):
        try:
            rule = request.user.sub_user_profile.shipping_rules.get(pk=rule_id)
        except Exception:
            raise NotFound()
        rule.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
