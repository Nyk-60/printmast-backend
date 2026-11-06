import json

from django.db.models import Q
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsPrintCenter, IsSubUser
from apps.orders.models import Order, OrderItem

from .models import MAX_RESHIP_REQUESTS_PER_ORDER, ReshipRequest
from .serializers import ReshipRequestSerializer
from .services import (
    approve_reship_request, create_reship_request, reject_reship_request,
    retry_shipstation_push,
)
from .validators import validate_evidence_file

# An order must have actually been through production at least once before
# it can be reshipped.
_RESHIPPABLE_STATUSES = (
    Order.Status.SENT_TO_PRINT, Order.Status.IN_PRODUCTION,
    Order.Status.SHIPPED, Order.Status.DELIVERED,
)

_ADDRESS_OVERRIDE_FIELDS = (
    'ship_to_name', 'ship_to_company', 'ship_to_address1', 'ship_to_address2',
    'ship_to_city', 'ship_to_state', 'ship_to_postal_code', 'ship_to_country',
    'ship_to_phone',
)


class ReshipRequestCreateView(APIView):
    permission_classes = (IsSubUser,)
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request):
        sub_user = request.user.sub_user_profile

        try:
            original_order = Order.objects.get(
                pk=request.data.get('original_order'), sub_user=sub_user, is_hidden=False,
            )
        except (Order.DoesNotExist, ValueError, TypeError):
            raise NotFound('Order not found.')

        if original_order.status not in _RESHIPPABLE_STATUSES:
            raise ValidationError({'detail': 'This order has not been sent to print yet.'})

        if original_order.spawned_by_reship_request.exists():
            raise ValidationError({
                'detail': "A reshipped order can't be reshipped again — only the original order can be.",
            })

        active_requests = original_order.reship_requests.exclude(
            status=ReshipRequest.Status.REJECTED,
        ).count()
        if active_requests >= MAX_RESHIP_REQUESTS_PER_ORDER:
            raise ValidationError({
                'detail': f'This order has already reached the maximum of {MAX_RESHIP_REQUESTS_PER_ORDER} reship requests.',
            })

        request_type = request.data.get('request_type') or ReshipRequest.RequestType.RESHIP
        if request_type not in ReshipRequest.RequestType.values:
            raise ValidationError({'request_type': 'Must be "reship" or "refund".'})

        if request_type == ReshipRequest.RequestType.REFUND:
            # A refund is always a PC-caused resolution — the end customer is
            # getting their money back rather than a replacement.
            fault_type = ReshipRequest.FaultType.PC
        else:
            fault_type = request.data.get('fault_type')
            if fault_type not in (ReshipRequest.FaultType.SELF, ReshipRequest.FaultType.PC):
                raise ValidationError({'fault_type': 'Must be "self" or "pc".'})

        try:
            raw_items = json.loads(request.data.get('items', '[]'))
        except (TypeError, ValueError):
            raise ValidationError({'items': 'Invalid item list.'})
        if not raw_items:
            raise ValidationError({'items': 'Select at least one item to reship.'})

        item_specs = []
        for raw in raw_items:
            try:
                order_item = original_order.items.get(pk=raw.get('order_item_id'))
            except (OrderItem.DoesNotExist, ValueError, TypeError):
                raise ValidationError({'items': 'One of the selected items was not found on this order.'})

            try:
                quantity = int(raw.get('quantity', 0))
            except (TypeError, ValueError):
                quantity = 0
            if quantity < 1 or quantity > order_item.quantity:
                raise ValidationError({
                    'items': "Cannot request more than the original order's item count.",
                })

            reason = raw.get('reason') or ''
            if fault_type == ReshipRequest.FaultType.PC:
                if reason and reason not in ReshipRequest.Reason.values:
                    raise ValidationError({'items': 'Invalid reship reason.'})
                if reason == ReshipRequest.Reason.LOST_SHIPMENT and not sub_user.can_report_lost_shipment:
                    raise PermissionDenied("You're not permitted to report a lost shipment.")
                if request_type == ReshipRequest.RequestType.REFUND and not reason:
                    raise ValidationError({'items': 'Select a reason for every item in a refund request.'})
            else:
                reason = ''

            # None = keep the original item's personalization; '' = deliberately cleared.
            personalization_override = raw.get('personalization_override')
            if personalization_override is not None and not isinstance(personalization_override, str):
                raise ValidationError({'items': 'Invalid personalization value.'})

            item_specs.append({
                'order_item': order_item, 'quantity': quantity, 'reason': reason,
                'personalization_override': personalization_override,
            })

        files = request.FILES.getlist('files')
        for f in files:
            validate_evidence_file(f)

        note = request.data.get('note', '') or ''

        try:
            raw_address = json.loads(request.data.get('address_override') or '{}')
        except (TypeError, ValueError):
            raise ValidationError({'address_override': 'Invalid address data.'})
        if not isinstance(raw_address, dict):
            raise ValidationError({'address_override': 'Invalid address data.'})
        address_override = {
            k: v.strip() for k, v in raw_address.items()
            if k in _ADDRESS_OVERRIDE_FIELDS and isinstance(v, str) and v.strip()
        } or None

        reship = create_reship_request(
            sub_user, original_order, fault_type, item_specs, note, files,
            address_override=address_override, request_type=request_type,
        )
        return Response(
            ReshipRequestSerializer(reship, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class ReshipRequestListView(generics.ListAPIView):
    """Role-aware: a sub-user sees their own requests; a PC sees every
    request against any of its sub-users' orders."""
    serializer_class = ReshipRequestSerializer
    permission_classes = (IsAuthenticated,)  # role-based scoping happens in get_queryset

    def get_queryset(self):
        user = self.request.user
        if getattr(user, 'is_print_center', False):
            qs = ReshipRequest.objects.filter(
                original_order__store__shipstation_account__print_center=user.print_center_profile,
            )
        elif getattr(user, 'is_sub_user', False):
            qs = ReshipRequest.objects.filter(sub_user=user.sub_user_profile)
        else:
            qs = ReshipRequest.objects.none()

        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)

        type_param = self.request.query_params.get('request_type')
        if type_param:
            qs = qs.filter(request_type=type_param)

        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(original_order__shipstation_order_number__icontains=search) |
                Q(original_order__customer_name__icontains=search)
            )

        return qs.select_related(
            'original_order', 'new_order', 'sub_user__user', 'resolved_by',
        ).prefetch_related('items__original_order_item__pc_variant', 'files')


class ReshipRequestApproveView(APIView):
    permission_classes = (IsPrintCenter,)

    def post(self, request, pk):
        reship = self._get(request, pk)
        approve_reship_request(reship, request.user)
        reship.refresh_from_db()
        return Response(ReshipRequestSerializer(reship, context={'request': request}).data)

    def _get(self, request, pk):
        try:
            return ReshipRequest.objects.get(
                pk=pk,
                original_order__store__shipstation_account__print_center=request.user.print_center_profile,
            )
        except ReshipRequest.DoesNotExist:
            raise NotFound()


class ReshipRequestRejectView(ReshipRequestApproveView):
    def post(self, request, pk):
        reship = self._get(request, pk)
        reject_reship_request(reship, request.user, note=request.data.get('note', '') or '')
        reship.refresh_from_db()
        return Response(ReshipRequestSerializer(reship, context={'request': request}).data)


class ReshipRequestRetryPushView(ReshipRequestApproveView):
    """Re-attempts the ShipStation push for a reship that was approved but
    never got a real ShipStation order (e.g. a transient API error). Blocked
    once synced — the push always creates a brand-new ShipStation order, so
    retrying a success would create a duplicate."""

    def post(self, request, pk):
        reship = self._get(request, pk)
        if reship.status != ReshipRequest.Status.APPROVED or not reship.new_order_id:
            raise ValidationError({'detail': 'Only an approved reship with a created order can be retried.'})
        if reship.shipstation_synced:
            raise ValidationError({'detail': 'This reship was already sent to ShipStation.'})
        reship = retry_shipstation_push(reship)
        return Response(ReshipRequestSerializer(reship, context={'request': request}).data)
