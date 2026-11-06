"""Reship Request workflow — mirrors apps/orders/views.py::send_to_print's
clone-then-debit shape, but clones an Order instead of debiting an existing
one, and the debit (if any) happens later, naturally, the normal way, when
the cloned order is itself sent to print."""
from django.db import transaction
from django.utils import timezone

from .models import ReshipRequest, ReshipRequestFile, ReshipRequestItem


def create_reship_request(sub_user, original_order, fault_type, item_specs, note, files,
                           address_override=None, request_type=ReshipRequest.RequestType.RESHIP):
    """`item_specs`: list of {'order_item': OrderItem, 'quantity': int, 'reason': str,
    'personalization_override': str|None} — already validated (quantity <= original,
    reason allowed) by the caller (the DRF view), so this trusts its input.

    Record creation (the ReshipRequest + its items/files) is atomic — a failure
    partway through never leaves an orphaned, unresolvable "pending" row. The
    ShipStation push that may follow is deliberately NOT inside this block (see
    _resolve_approved): it's a slow network call, and it must not hold the
    original row-creation transaction open."""
    with transaction.atomic():
        request_reason = ''
        if fault_type == ReshipRequest.FaultType.PC and item_specs:
            request_reason = item_specs[0].get('reason', '') or ''

        reship = ReshipRequest.objects.create(
            original_order=original_order,
            sub_user=sub_user,
            request_type=request_type,
            fault_type=fault_type,
            reason=request_reason,
            note=note,
            address_override=address_override,
        )

        for spec in item_specs:
            ReshipRequestItem.objects.create(
                reship_request=reship,
                original_order_item=spec['order_item'],
                quantity=spec['quantity'],
                reason=spec.get('reason', '') or '',
                personalization_override=spec.get('personalization_override'),
            )

        for f in files:
            ReshipRequestFile.objects.create(reship_request=reship, file=f)

        from apps.orders.models import OrderEvent
        from apps.orders.services import log_event
        kind = 'Refund' if request_type == ReshipRequest.RequestType.REFUND else 'Reship'
        log_event(
            original_order, OrderEvent.EventType.RESHIP_REQUESTED, actor=sub_user.user,
            description=f'{kind} requested ({reship.get_fault_type_display()})',
        )

    # A refund always needs PC approval — never auto-processed, even for a
    # sub-user with the reship-bypass permission (that permission is about
    # trusting a sub-user's own replacement judgment, not about moving money).
    auto = request_type == ReshipRequest.RequestType.RESHIP and (
        fault_type == ReshipRequest.FaultType.SELF or (
            fault_type == ReshipRequest.FaultType.PC and sub_user.can_reship_without_pc_approval
        )
    )
    if auto:
        reship.auto_approved = fault_type == ReshipRequest.FaultType.PC
        reship.save(update_fields=['auto_approved'])
        _resolve_approved(reship)
    else:
        _notify_pc_new_request(reship)

    return reship


@transaction.atomic
def _create_clone(reship, resolved_by=None):
    """Clones the original order (selected items only, at their edited
    quantities, with any address/personalization corrections applied) into a
    brand-new Order that re-enters the normal pipeline from scratch — matching,
    design, send-to-print, label purchase all run completely unmodified.
    Free-of-charge PC-fault reships are flagged via Order.is_free_reship, read
    by send_to_print later.

    Deliberately does NOT touch item matching (pc_variant) or design — those
    are learned/auto-matched per raw variation signature for the store, and
    editing them here (instead of through the normal matching screen on the
    new order) would risk corrupting that auto-match table for unrelated,
    correctly-ordered future orders."""
    from apps.designs.models import OrderItemDesign
    from apps.orders.models import Order, OrderEvent, OrderItem
    from apps.orders.services import log_event, refresh_order_status

    original = reship.original_order
    is_pc_fault = reship.fault_type == ReshipRequest.FaultType.PC
    addr = reship.address_override or {}

    prior_approved = ReshipRequest.objects.filter(
        original_order=original, status=ReshipRequest.Status.APPROVED,
    ).exclude(pk=reship.pk).count()
    suffix = '-Reship' if prior_approved == 0 else f'-Reship-{prior_approved + 1}'

    new_order = Order.objects.create(
        sub_user=original.sub_user,
        store=original.store,
        # Placeholder, unique via the reship PK — overwritten with the real
        # ShipStation-assigned ID once the push below completes.
        shipstation_order_id=f'{original.shipstation_order_id}-reship-{reship.pk}',
        shipstation_order_number=f'{original.shipstation_order_number}{suffix}',
        external_order_number=(
            f'{original.external_order_number}{suffix}' if original.external_order_number else ''
        ),
        customer_name=original.customer_name,
        customer_email=original.customer_email,
        ship_to_name=addr.get('ship_to_name', original.ship_to_name),
        ship_to_company=addr.get('ship_to_company', original.ship_to_company),
        ship_to_address1=addr.get('ship_to_address1', original.ship_to_address1),
        ship_to_address2=addr.get('ship_to_address2', original.ship_to_address2),
        ship_to_city=addr.get('ship_to_city', original.ship_to_city),
        ship_to_state=addr.get('ship_to_state', original.ship_to_state),
        ship_to_postal_code=addr.get('ship_to_postal_code', original.ship_to_postal_code),
        ship_to_country=addr.get('ship_to_country', original.ship_to_country),
        ship_to_phone=addr.get('ship_to_phone', original.ship_to_phone),
        # A corrected address hasn't been verified by ShipStation yet — only
        # carry over the original's verified indicator when nothing changed.
        address_status=original.address_status if not addr else Order.AddressStatus.UNKNOWN,
        address_verified=original.address_verified if not addr else False,
        ordered_at=timezone.now(),
        requested_service=original.requested_service,
        internal_note=reship.note,
        status=Order.Status.NEW,
        is_free_reship=is_pc_fault,
    )

    for item in reship.items.all():
        src = item.original_order_item
        personalization = (
            item.personalization_override if item.personalization_override is not None
            else src.personalization
        )
        new_item = OrderItem.objects.create(
            order=new_order,
            marketplace_sku=src.marketplace_sku,
            raw_product_name=src.raw_product_name,
            raw_options=src.raw_options,
            personalization=personalization,
            image_url=src.image_url,
            quantity=item.quantity,
            unit_price=src.unit_price,
            pc_variant=src.pc_variant,
            matched_at=src.matched_at,
        )
        # Reuses the SAME stored file (no copy) — same shared-file convention
        # as auto_match_design(); file_in_use() already protects it from
        # deletion while any row still references it.
        for design in src.designs.all():
            OrderItemDesign.objects.create(
                order_item=new_item,
                print_area_name=design.print_area_name,
                file=design.file,
                file_name=design.file_name,
                width_px=design.width_px,
                height_px=design.height_px,
                sku_design=design.sku_design,
            )

    refresh_order_status(new_order)

    reship.new_order = new_order
    reship.status = ReshipRequest.Status.APPROVED
    reship.resolved_at = timezone.now()
    if resolved_by:
        reship.resolved_by = resolved_by
    reship.save(update_fields=['new_order', 'status', 'resolved_at', 'resolved_by'])

    log_event(
        original, OrderEvent.EventType.RESHIP_APPROVED,
        actor=resolved_by, description=f'Reship order {new_order.shipstation_order_number} created',
    )

    from apps.accounts.models import Notification
    Notification.objects.create(
        user=reship.sub_user.user,
        title='Reship request approved',
        body=f'Your reship request for order {original.shipstation_order_number} was approved.',
        link=f'/reship-requests/{reship.pk}',
    )

    return new_order


def _push_to_shipstation(reship, new_order):
    """Pushes the cloned order to ShipStation synchronously, right in the
    request/response cycle, so the caller (create or approve) gets an honest
    success/failure result immediately instead of a silent background
    best-effort. Deliberately outside _create_clone's transaction — a slow
    network call must never hold that transaction open."""
    from apps.shipping.tasks import push_reship_order_to_shipstation

    try:
        push_reship_order_to_shipstation(new_order.pk)
        reship.shipstation_synced = True
        reship.shipstation_sync_error = ''
    except Exception as exc:
        reship.shipstation_synced = False
        reship.shipstation_sync_error = str(exc)[:500]
    reship.save(update_fields=['shipstation_synced', 'shipstation_sync_error'])

    if not reship.shipstation_synced:
        from apps.accounts.models import Notification
        pc_user = reship.original_order.store.shipstation_account.print_center.user
        Notification.objects.create(
            user=pc_user,
            title='Reship not sent to ShipStation',
            body=(f'Reship order {new_order.shipstation_order_number} was created but could '
                  f'not be sent to ShipStation: {reship.shipstation_sync_error}'),
            link=f'/reship-requests/{reship.pk}',
        )


def _resolve_approved(reship, resolved_by=None):
    new_order = _create_clone(reship, resolved_by)
    _push_to_shipstation(reship, new_order)
    return new_order


def retry_shipstation_push(reship):
    """Re-attempts a previously failed push. Guarded by the caller against
    retrying an already-synced reship — push_reship_order_to_shipstation
    always creates a brand-new ShipStation order (it never updates in place),
    so calling it twice on a success would create a duplicate."""
    _push_to_shipstation(reship, reship.new_order)
    return reship


@transaction.atomic
def _resolve_refund(reship, resolved_by=None):
    """Credits the sub-user's balance for the selected items' original
    fulfillment + extra-print cost (recomputed the same way send_to_print
    originally charged it — apps.orders.services.calculate_order_cost_lines),
    plus shipping only when every billable item on the order is part of this
    refund (a partial refund never includes shipping — the package still
    went out). One lump-sum BalanceTransaction, same convention as
    apps.orders.services.refund_order_debit."""
    from decimal import ROUND_HALF_UP, Decimal

    from apps.accounts.models import BalanceTransaction, Notification, SubUserProfile
    from apps.orders.models import OrderEvent
    from apps.orders.services import billable_items, log_event

    _CENTS = Decimal('0.01')
    original = reship.original_order
    sub_user = SubUserProfile.objects.select_for_update().get(pk=reship.sub_user_id)

    extra_price = Decimal('0')
    if sub_user.extra_print_area_enabled:
        extra_price = sub_user.extra_print_area_price or Decimal('0')

    total = Decimal('0')
    refunded_item_ids = set()
    for ri in reship.items.select_related('original_order_item__pc_variant'):
        src = ri.original_order_item
        refunded_item_ids.add(src.id)
        variant = src.pc_variant
        if not variant:
            continue
        total += (variant.cost * ri.quantity).quantize(_CENTS, rounding=ROUND_HALF_UP)
        if extra_price > 0:
            extra_units = max(0, src.designs.count() - 1) * ri.quantity
            if extra_units:
                total += (extra_units * extra_price).quantize(_CENTS, rounding=ROUND_HALF_UP)

    billable_ids = {i.id for i in billable_items(original)}
    if billable_ids and billable_ids.issubset(refunded_item_ids):
        total += (original.shipping_cost or Decimal('0'))
    total = total.quantize(_CENTS, rounding=ROUND_HALF_UP)

    if total > 0:
        sub_user.balance += total
        sub_user.save(update_fields=['balance'])
        BalanceTransaction.objects.create(
            sub_user=sub_user,
            type=BalanceTransaction.Type.REFUND,
            amount=total,
            category='',  # covers item + extra-print (+ shipping, if the whole order) in one line
            order=original,
            order_reference=original.shipstation_order_id,
            order_number=original.shipstation_order_number or original.external_order_number,
            description=(
                f'Refund for order {original.shipstation_order_number}'
                + (f' ({reship.get_reason_display()})' if reship.reason else '')
            ),
            created_by=resolved_by,
        )

    reship.refund_amount = total
    reship.status = ReshipRequest.Status.APPROVED
    reship.resolved_at = timezone.now()
    if resolved_by:
        reship.resolved_by = resolved_by
    reship.save(update_fields=['refund_amount', 'status', 'resolved_at', 'resolved_by'])

    log_event(
        original, OrderEvent.EventType.RESHIP_APPROVED, actor=resolved_by,
        description=f'Refund approved: ${total}',
    )

    Notification.objects.create(
        user=reship.sub_user.user,
        title='Refund request approved',
        body=f'${total} was credited to your balance for order {original.shipstation_order_number}.',
        link=f'/reship-requests/{reship.pk}',
    )

    return reship


def approve_reship_request(reship, pc_user):
    """PC-driven approval — also how a Rejected request is flipped back.
    Idempotent: a no-op if already approved (never double-creates the clone
    or double-credits a refund)."""
    if reship.status == ReshipRequest.Status.APPROVED:
        return reship
    if reship.request_type == ReshipRequest.RequestType.REFUND:
        return _resolve_refund(reship, resolved_by=pc_user)
    return _resolve_approved(reship, resolved_by=pc_user)


def reject_reship_request(reship, pc_user, note=''):
    from apps.orders.models import OrderEvent
    from apps.orders.services import log_event

    reship.status = ReshipRequest.Status.REJECTED
    reship.resolved_at = timezone.now()
    reship.resolved_by = pc_user
    reship.rejection_note = note
    reship.save(update_fields=['status', 'resolved_at', 'resolved_by', 'rejection_note'])

    kind = 'Refund' if reship.request_type == ReshipRequest.RequestType.REFUND else 'Reship'
    log_event(
        reship.original_order, OrderEvent.EventType.RESHIP_REJECTED, actor=pc_user,
        description=note or f'{kind} request rejected',
    )

    from apps.accounts.models import Notification
    Notification.objects.create(
        user=reship.sub_user.user,
        title=f'{kind} request rejected',
        body=note or f'Your {kind.lower()} request for order {reship.original_order.shipstation_order_number} was rejected.',
        link=f'/reship-requests/{reship.pk}',
    )
    return reship


def _notify_pc_new_request(reship):
    from apps.accounts.models import Notification
    pc_user = reship.original_order.store.shipstation_account.print_center.user
    requester = reship.sub_user.user.full_name or reship.sub_user.user.email
    kind = 'refund' if reship.request_type == ReshipRequest.RequestType.REFUND else 'reship'
    Notification.objects.create(
        user=pc_user,
        title=f'New {kind} request',
        body=f'{requester} requested a {kind} for order '
             f'{reship.original_order.shipstation_order_number}.',
        link=f'/reship-requests/{reship.pk}',
    )
