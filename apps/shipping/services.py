"""Multi-provider rate shopping and idempotent label purchase.

Compares every label provider the print center has marked active (ShipStation,
ByeLabel, ...) for an order's package, restricted to the (carrier, service)
combinations the print center has allowed for that service category, and buys
from whichever is cheapest. Purchase itself is guarded so a retried batch or
a race between two workers can never buy a second real label for the same
order — see purchase_label_for_order().
"""
import logging

from django.db import transaction

logger = logging.getLogger(__name__)


def _order_ship_to_address(order):
    """Recipient address for providers other than ShipStation, with text cleaned
    of emoji/symbols the label APIs may reject (see sanitize_label_text)."""
    from .utils import sanitize_label_text as clean

    return {
        'name': clean(order.ship_to_name),
        'company': clean(order.ship_to_company),
        'phone': '',
        'street1': clean(order.ship_to_address1),
        'street2': clean(order.ship_to_address2),
        'city': clean(order.ship_to_city),
        'state': clean(order.ship_to_state),
        'zip_code': clean(order.ship_to_postal_code),
        'country': clean(order.ship_to_country) or 'US',
    }


def _order_customs_items(order):
    """Line items for providers that require them (ByeLabel does, even domestic)."""
    from apps.orders.services import billable_items

    items = []
    for i in billable_items(order):
        name = i.pc_variant.pc_product.name if i.pc_variant_id else i.raw_product_name
        items.append({
            'description': name,
            'quantity': i.quantity,
            'code': i.marketplace_sku,
            'total_price': float(i.unit_price) * i.quantity,
        })
    return items


def _pc_ship_from_address(pc, store):
    """Sender address for a label — sourced from the STORE's own synced
    ShipStation warehouse address, not a single PC-wide address, since
    different stores can genuinely ship from different locations."""
    from .utils import sanitize_label_text as clean

    return {
        'name': clean(store.ship_from_name or pc.ship_from_name or pc.company_name),
        'company': clean(store.ship_from_company),
        'phone': store.ship_from_phone or '',
        'street1': clean(store.ship_from_street1),
        'street2': clean(store.ship_from_street2),
        'city': clean(store.ship_from_city),
        'state': clean(store.ship_from_state),
        'zip_code': clean(store.ship_from_postal_code),
        'country': clean(store.ship_from_country) or 'US',
    }


def _group_by_provider(options):
    groups = {}
    for o in options:
        groups.setdefault(o.provider, []).append(o)
    return groups


def shop_rates_for_order(order, weight_oz, dimensions):
    """Compare rates across every active provider for this PC, restricted to
    the (carrier, service) combos allowed for the order's service category.

    Returns the cheapest {account, provider, carrier_code, service_code,
    service_name, cost} dict, or None if nothing is available.
    """
    from .byelabel_client import ByeLabelClient, ByeLabelError
    from .client import ShipStationClient, ShipStationError
    from .models import LabelProviderAccount, ShippingServiceOption
    from .utils import categorize_requested_service

    pc = order.store.shipstation_account.print_center
    category = categorize_requested_service(order.requested_service)

    options = list(ShippingServiceOption.objects.filter(
        print_center=pc, category=category, is_active=True,
    ))
    if not options:
        return None

    if not order.store.ship_from_configured:
        logger.warning(
            'No ship-from address synced for store %s (order %s) — cannot rate-shop.',
            order.store_id, order.id,
        )
        return None

    # One active account per provider is compared (a print center connecting
    # two accounts of the same provider is not a supported case yet).
    accounts = {
        a.provider: a for a in
        LabelProviderAccount.objects.filter(print_center=pc, is_active=True)
    }
    from_postal = order.store.ship_from_postal_code

    best = None
    for provider_key, provider_options in _group_by_provider(options).items():
        account = accounts.get(provider_key)
        if not account:
            continue
        allowed = {(o.carrier_code, o.service_code) for o in provider_options}

        try:
            if provider_key == LabelProviderAccount.Provider.SHIPSTATION:
                if not account.shipstation_account_id:
                    continue
                client = ShipStationClient(account.shipstation_account)
            elif provider_key == LabelProviderAccount.Provider.BYELABEL:
                client = ByeLabelClient(account)
            else:
                continue
            rates = client.get_rates(
                from_postal=from_postal, to_postal=order.ship_to_postal_code,
                to_country=order.ship_to_country or 'US', weight_oz=weight_oz, dimensions=dimensions,
            )
        except (ShipStationError, ByeLabelError) as exc:
            logger.warning('Rate fetch failed for provider %s on order %s: %s', provider_key, order.id, exc)
            continue

        for r in rates:
            key = (r.get('carrierCode'), r.get('serviceCode'))
            if key not in allowed:
                continue
            cost = float(r.get('shipmentCost') or 0) + float(r.get('otherCost') or 0)
            if best is None or cost < best['cost']:
                best = {
                    'account': account, 'provider': provider_key,
                    'carrier_code': key[0], 'service_code': key[1],
                    'service_name': r.get('serviceName'), 'cost': cost,
                }
    return best


def purchase_label_for_order(order, batch_order=None):
    """Idempotent, rate-shopped label purchase for one order.

    Returns the decoded label image bytes if a ShipStation label was just
    purchased and returned image data, otherwise None — this covers success
    without an image (ByeLabel, or format not returned), an idempotent no-op
    (order already had a label), and failure alike. Callers must not infer
    success/failure from the return value: check order.labeled_at, or read
    the LabelPurchaseAttempt / OrderEvent trail this function always writes.
    """
    from django.utils import timezone as tz

    from apps.orders.models import Order
    from apps.orders.services import log_event

    from .byelabel_client import ByeLabelClient
    from .client import ShipStationClient, ShipStationError
    from .models import LabelProviderAccount, LabelPurchaseAttempt
    from .utils import get_order_shipping_config

    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        already_labeled = locked.labeled_at is not None
    if already_labeled:
        # Never re-buy — but a previous run may have bought the label and failed
        # to tell ShipStation, so retry that (idempotent) notification here.
        notify_shipstation_shipped(order)
        return None

    weight_oz, dimensions = get_order_shipping_config(order)
    if not weight_oz:
        return None

    best = shop_rates_for_order(order, weight_oz, dimensions)
    if not best:
        if not order.store.ship_from_configured:
            logger.warning('No ship-from address configured for store %s (order %s)', order.store_id, order.id)
            log_event(
                order, 'label_failed',
                description=f'No ship-from address configured for store "{order.store.name}" — '
                            'sync it from ShipStation before this order can be labeled.',
            )
        else:
            logger.warning('No rate available from any active provider for order %s', order.id)
            log_event(order, 'label_failed', description='No shipping rate available from any active provider')
        return None

    account = best['account']
    attempt = LabelPurchaseAttempt.objects.create(
        order=order, provider_account=account, status=LabelPurchaseAttempt.Status.REQUESTED,
        carrier_code=best['carrier_code'], service_code=best['service_code'], cost=round(best['cost'], 2),
    )
    log_event(
        order, 'label_attempt',
        description=f'{account.get_provider_display()} — {best["carrier_code"]}/{best["service_code"]} (${best["cost"]:.2f})',
    )

    pc = order.store.shipstation_account.print_center
    label_data = None
    try:
        if best['provider'] == LabelProviderAccount.Provider.SHIPSTATION:
            client = ShipStationClient(account.shipstation_account)
            try:
                client.update_order_shipping_config(order, weight_oz=weight_oz, dimensions=dimensions)
            except ShipStationError as exc:
                logger.warning('Pre-label shipping config push failed for order %s: %s', order.id, exc)
            result = client.create_label(
                shipstation_order_id=order.shipstation_order_id,
                carrier_code=best['carrier_code'], service_code=best['service_code'],
            )
            tracking = result.get('trackingNumber', '')
            shipment_id = str(result.get('shipmentId', ''))
            cost = result.get('shipmentCost') or best['cost']
            label_url = (result.get('labelDownload') or {}).get('href', '')
            label_data = result.get('labelData')
        elif best['provider'] == LabelProviderAccount.Provider.BYELABEL:
            client = ByeLabelClient(account)
            result = client.create_label(
                carrier_code=best['carrier_code'], service_code=best['service_code'],
                packaging_code='package',
                ship_from=_pc_ship_from_address(pc, order.store),
                ship_to=_order_ship_to_address(order),
                weight_oz=weight_oz, dimensions=dimensions,
                items=_order_customs_items(order),
            )
            # Response modelled on GET /shipment/{id} (verified on existing shipments):
            # id, tracking_number, cost/total_cost, label{format, data=base64 PDF}.
            # A successful create call has not been observed yet — extract defensively
            # and fall back to the quoted rate rather than crash on a different shape.
            tracking = result.get('tracking_number') or result.get('trackingNumber') or ''
            shipment_id = str(result.get('id') or result.get('shipment_id') or '')
            cost = result.get('total_cost') or result.get('cost') or best['cost']
            label = result.get('label')
            label_url = result.get('label_url') or (label.get('url') if isinstance(label, dict) else '') or ''
            if not tracking:
                # A label was (probably) bought but we can't identify it: never mark
                # it confirmed with no tracking — surface for manual reconciliation.
                raise ValueError(f'ByeLabel create-label returned no tracking number: {str(result)[:200]}')
        else:
            raise ValueError(f'Unknown provider {best["provider"]}')
    except Exception as exc:
        attempt.status = LabelPurchaseAttempt.Status.FAILED
        attempt.error_message = str(exc)[:2000]
        attempt.save(update_fields=['status', 'error_message'])
        logger.warning('Label purchase failed for order %s via %s: %s', order.id, best['provider'], exc)
        log_event(order, 'label_failed', description=f'{account.get_provider_display()}: {exc}'[:280])
        return None

    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.labeled_at is not None:
            # Another attempt confirmed while this one was in flight — we DID
            # just buy a real, separate label with no DB record for it. This
            # must never be silently discarded.
            attempt.status = LabelPurchaseAttempt.Status.NEEDS_REVIEW
            attempt.tracking_number = tracking
            attempt.label_url = label_url
            attempt.error_message = 'Duplicate: order already had a confirmed label when this attempt completed.'
            attempt.save(update_fields=['status', 'tracking_number', 'label_url', 'error_message'])
            logger.error(
                'DUPLICATE LABEL PURCHASED for order %s via %s — tracking %s, cost %s. '
                'Needs manual void/refund review.',
                order.id, best['provider'], tracking, cost,
            )
            log_event(
                order, 'label_failed',
                description=f'Duplicate label purchased (tracking {tracking}) — needs manual review',
            )
            return None

        locked.tracking_number = tracking
        locked.shipstation_shipment_id = shipment_id
        locked.label_cost = cost
        locked.label_url = label_url
        locked.labeled_at = tz.now()
        locked.save(update_fields=[
            'tracking_number', 'shipstation_shipment_id', 'label_cost', 'label_url', 'labeled_at',
        ])
        order.tracking_number = tracking
        order.label_cost = cost

    attempt.status = LabelPurchaseAttempt.Status.CONFIRMED
    attempt.tracking_number = tracking
    attempt.label_url = label_url
    attempt.confirmed_at = tz.now()
    attempt.save(update_fields=['status', 'tracking_number', 'label_url', 'confirmed_at'])

    log_event(
        order, 'label_confirmed',
        description=f'{account.get_provider_display()} — tracking {tracking}, cost ${cost}',
    )

    if batch_order is not None:
        batch_order.label_purchased = True
        batch_order.label_cost = cost
        batch_order.save(update_fields=['label_purchased', 'label_cost'])

    notify_shipstation_shipped(order)

    if label_data:
        import base64
        return base64.b64decode(label_data)
    return None


# ByeLabel carrier → ShipStation carrier codes to try, in order. ShipStation
# validates carrierCode on markasshipped, and which of these a given account
# accepts for a carrier it isn't connected to is not documented.
_SHIPSTATION_CARRIER_CANDIDATES = {
    'usps': ('usps', 'stamps_com'),
    'ups': ('ups', 'ups_walleted'),
    'fedex': ('fedex', 'fedex_walleted'),
}


def notify_shipstation_shipped(order):
    """Tell ShipStation an order shipped, so it notifies the marketplace with
    tracking — for labels bought from a provider other than ShipStation.

    Idempotent and never raises: labels bought through ShipStation itself are
    skipped (it already knows), a confirmed attempt is notified at most once,
    and an order ShipStation already shows as shipped is only recorded. A
    failure is stored on the attempt and logged, and is retried the next time
    purchase_label_for_order runs for this order.
    """
    from django.utils import timezone as tz

    from apps.orders.services import log_event

    from .client import ShipStationClient, ShipStationError
    from .models import LabelProviderAccount, LabelPurchaseAttempt

    attempt = (
        LabelPurchaseAttempt.objects
        .filter(order=order, status=LabelPurchaseAttempt.Status.CONFIRMED)
        .select_related('provider_account').first()
    )
    if (not attempt or attempt.shipstation_notified_at
            or not attempt.provider_account
            or attempt.provider_account.provider == LabelProviderAccount.Provider.SHIPSTATION):
        return

    try:
        client = ShipStationClient(order.store.shipstation_account)
        if (client.get_order(order.shipstation_order_id).get('orderStatus') or '').lower() == 'shipped':
            attempt.shipstation_notified_at = tz.now()
            attempt.notify_error = ''
            attempt.save(update_fields=['shipstation_notified_at', 'notify_error'])
            log_event(order, 'shipstation_notified', description='Already shipped in ShipStation — nothing to send')
            return

        family = (attempt.carrier_code or '').lower()
        candidates = _SHIPSTATION_CARRIER_CANDIDATES.get(family, (family,))
        last_error = None
        for carrier_code in candidates:
            try:
                client.mark_as_shipped(
                    shipstation_order_id=order.shipstation_order_id,
                    carrier_code=carrier_code,
                    tracking_number=attempt.tracking_number,
                    ship_date=tz.now().date().isoformat(),
                    notify_customer=False,
                    notify_sales_channel=True,
                )
                break
            except ShipStationError as exc:
                last_error = exc
        else:
            raise last_error or ShipStationError('No carrier code to try')
    except Exception as exc:
        attempt.notify_error = str(exc)[:2000]
        attempt.save(update_fields=['notify_error'])
        logger.warning('ShipStation notify failed for order %s: %s', order.id, exc)
        log_event(
            order, 'shipstation_notify_failed',
            description=f'Marketplace not notified yet — will retry: {exc}'[:280],
        )
        return

    attempt.shipstation_notified_at = tz.now()
    attempt.notify_error = ''
    attempt.save(update_fields=['shipstation_notified_at', 'notify_error'])
    log_event(
        order, 'shipstation_notified',
        description=f'Marked shipped in ShipStation (tracking {attempt.tracking_number}); marketplace notified',
    )
