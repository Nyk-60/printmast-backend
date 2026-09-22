"""Multi-provider rate shopping and idempotent label purchase.

Compares every label provider the print center has marked active (ShipStation,
ByeLabel, ...) for an order's package, restricted to the (carrier, service)
combinations the print center has allowed for that service category, and buys
from whichever is cheapest. Purchase itself is guarded so a retried batch or
a race between two workers can never buy a second real label for the same
order — see purchase_label_for_order().
"""
import logging
from dataclasses import dataclass

from django.db import transaction

logger = logging.getLogger(__name__)


def _order_ship_to_address(order):
    """Recipient address for providers other than ShipStation, with text cleaned
    of emoji/symbols the label APIs may reject (see sanitize_label_text)."""
    from .utils import sanitize_label_text as clean

    return {
        'name': clean(order.ship_to_name),
        'company': clean(order.ship_to_company),
        'phone': clean(order.ship_to_phone) or '0000000000',
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


def shop_all_rates(pc, store, category, *, to_postal, to_country, weight_oz, dimensions,
                    carrier_code=None, label=''):
    """Every rate quote across every active provider for this PC, restricted to the
    (carrier, service) combos allowed for that service category.

    Used both for the real, money-spending purchase decision (shop_rates_for_order)
    and for the price shown to the sub-user BEFORE send-to-print (RatesView, the
    auto-select and refresh tasks) — so the displayed/charged price already reflects
    whichever provider is actually cheapest, not just ShipStation.

    Returns a list of {account, provider, carrier_code, service_code, service_name,
    cost}, cheapest first. A provider erroring out (no rates for the route, bad
    credentials, timeout, ...) is skipped, not fatal — the other provider's quotes
    still come back. `label` only identifies what was being rate-shopped in the log
    line (an order id, or a sub-user's default-rate check).
    """
    from .byelabel_client import ByeLabelClient, ByeLabelError
    from .client import ShipStationClient, ShipStationError
    from .models import LabelProviderAccount, ShippingServiceOption
    from .utils import real_service_key

    options = list(ShippingServiceOption.objects.filter(
        print_center=pc, category=category, is_active=True,
    ))
    if not options:
        return []

    if not store.ship_from_configured:
        logger.warning('No ship-from address synced for store %s (%s) — cannot rate-shop.', store.id, label)
        return []

    # One active account per provider is compared (a print center connecting
    # two accounts of the same provider is not a supported case yet).
    accounts = {
        a.provider: a for a in
        LabelProviderAccount.objects.filter(print_center=pc, is_active=True)
    }
    from_postal = store.ship_from_postal_code

    quotes = []
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
                from_postal=from_postal, to_postal=to_postal, to_country=to_country or 'US',
                weight_oz=weight_oz, dimensions=dimensions, carrier_code=carrier_code,
            )
        except (ShipStationError, ByeLabelError) as exc:
            logger.warning('Rate fetch failed for provider %s (%s): %s', provider_key, label, exc)
            continue

        for r in rates:
            key = (r.get('carrierCode'), r.get('serviceCode'))
            if key not in allowed:
                continue
            cost = float(r.get('shipmentCost') or 0) + float(r.get('otherCost') or 0)
            quotes.append({
                'account': account, 'provider': provider_key,
                'carrier_code': key[0], 'service_code': key[1],
                'service_name': r.get('serviceName'), 'cost': cost,
                'real_key': real_service_key(*key),
            })

    quotes.sort(key=lambda q: q['cost'])
    return quotes


def shop_rates_for_order(order, weight_oz, dimensions):
    """Compare rates across every active provider for this PC, restricted to
    the (carrier, service) combos allowed for the order's service category.

    Returns the cheapest {account, provider, carrier_code, service_code,
    service_name, cost} dict, or None if nothing is available.
    """
    from .utils import effective_shipping_category

    pc = order.store.shipstation_account.print_center
    category = effective_shipping_category(order)
    quotes = shop_all_rates(
        pc, order.store, category,
        to_postal=order.ship_to_postal_code, to_country=order.ship_to_country,
        weight_oz=weight_oz, dimensions=dimensions, label=f'order {order.id}',
    )
    return quotes[0] if quotes else None


class LabelOutcomeUnknown(Exception):
    """A label may or may not have been bought (e.g. the response had no tracking)."""


@dataclass
class PurchaseOutcome:
    """Result of one order's label purchase — recorded, never inferred from side effects.

    status: 'labeled' | 'failed' | 'needs_review'
    kind:   BatchOrder.ErrorKind value for non-labeled results ('data', 'account',
            'transient', 'ambiguous', 'other').
    """
    status: str
    kind: str = ''
    message: str = ''

    @property
    def ok(self):
        return self.status == 'labeled'


_ACCOUNT_HINTS = (
    'balance', 'insufficient', 'funds', 'authentication', 'unauthorized', 'not authorized',
    'forbidden', 'credentials', 'terms and conditions', 'plan', 'label limit', 'reauthorize',
    'workspace', 'suspended', 'disabled',
)
_DATA_HINTS = (
    'address', 'zip', 'postal', 'state', 'city', 'phone', 'name', 'street', 'weight', 'dimension',
    'invalid character', 'xml schema', 'customer reference', 'required', 'not allowed', 'must be',
    'items', 'country', 'validation',
)


def classify_label_error(exc):
    """Map an exception from a label-purchase call to a BatchOrder.ErrorKind.

    The distinction that matters most is 'ambiguous': after a timeout, dropped
    connection or 5xx the provider may already have created (and charged) the
    label, so the order must NOT be bought again automatically.
    """
    from requests import RequestException

    from .byelabel_client import ByeLabelError
    from .client import ShipStationError

    if isinstance(exc, LabelOutcomeUnknown) or isinstance(exc, RequestException):
        return 'ambiguous'
    if isinstance(exc, (ShipStationError, ByeLabelError)):
        status = getattr(exc, 'status_code', None)
        text = str(exc).lower()
        code = getattr(exc, 'code', '') or ''
        if status is None or status >= 500 or 'non-json' in text:
            return 'ambiguous'
        if status == 429:
            return 'transient'
        if status in (401, 403) or code in ('INVALID_TOKEN', 'MISSING_WORKSPACE_ID'):
            return 'account'
        if code == 'VALIDATION_ERROR':
            return 'data'
        if any(h in text for h in _ACCOUNT_HINTS):
            return 'account'
        if any(h in text for h in _DATA_HINTS):
            return 'data'
    return 'other'


def _finalize_purchase(order, attempt, account, *, tracking, shipment_id, cost, label_url,
                       label_data, batch_order=None):
    """Record a label that now certainly exists: order fields (under a row lock, with
    duplicate detection), the attempt, the audit event, the stored label file and the
    ShipStation shipped-notification for non-ShipStation labels."""
    from django.utils import timezone as tz

    from apps.orders.models import Order
    from apps.orders.services import log_event

    from .models import LabelPurchaseAttempt

    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.labeled_at is not None:
            # Another attempt confirmed while this one was in flight — a real,
            # separate label now exists with no DB record. Never discard silently.
            attempt.status = LabelPurchaseAttempt.Status.NEEDS_REVIEW
            attempt.tracking_number = tracking
            attempt.label_url = label_url
            attempt.error_message = 'Duplicate: order already had a confirmed label when this attempt completed.'
            attempt.save(update_fields=['status', 'tracking_number', 'label_url', 'error_message'])
            logger.error(
                'DUPLICATE LABEL PURCHASED for order %s via %s — tracking %s, cost %s. '
                'Needs manual void/refund review.', order.id, account, tracking, cost,
            )
            log_event(
                order, 'label_failed',
                description=f'Duplicate label purchased (tracking {tracking}) — needs manual review',
            )
            return PurchaseOutcome(
                'needs_review', 'ambiguous', f'Duplicate label (tracking {tracking}) — void it with the provider.',
            )

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
    attempt.error_message = ''
    attempt.confirmed_at = tz.now()
    attempt.save(update_fields=['status', 'tracking_number', 'label_url', 'error_message', 'confirmed_at'])

    log_event(
        order, 'label_confirmed',
        description=f'{account.get_provider_display()} — tracking {tracking}, cost ${cost}',
    )

    if batch_order is not None:
        batch_order.label_purchased = True
        batch_order.label_cost = cost
        batch_order.save(update_fields=['label_purchased', 'label_cost'])

    if label_data:
        from .labels import decode_label_data, store_label_file
        try:
            store_label_file(order, decode_label_data(label_data), tracking)
        except Exception:
            logger.exception('Could not decode label data for order %s', order.id)

    notify_shipstation_shipped(order)
    return PurchaseOutcome('labeled')


_STALE_ATTEMPT_MINUTES = 10


def purchase_label_for_order(order, batch_order=None):
    """Idempotent, rate-shopped label purchase for one order -> PurchaseOutcome.

    Safe to call repeatedly: an order that already has a label is never re-bought,
    and neither is one whose earlier attempt has an unknown outcome (see
    classify_label_error / reconcile_attempt). The label file is stored on
    order.label_file; every attempt and result is also written to
    LabelPurchaseAttempt / OrderEvent.
    """
    from datetime import timedelta

    from django.db.models import Q
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
        return PurchaseOutcome('labeled')

    # An earlier attempt whose outcome is unknown must be resolved (reconcile_attempt
    # or a person) before buying again — otherwise a retry could pay twice.
    cutoff = tz.now() - timedelta(minutes=_STALE_ATTEMPT_MINUTES)
    open_attempts = LabelPurchaseAttempt.objects.filter(order=order).filter(
        Q(status=LabelPurchaseAttempt.Status.NEEDS_REVIEW)
        | Q(status=LabelPurchaseAttempt.Status.REQUESTED)
    )
    if open_attempts.filter(
        Q(status=LabelPurchaseAttempt.Status.NEEDS_REVIEW) | Q(created_at__lt=cutoff)
    ).exists():
        return PurchaseOutcome(
            'needs_review', 'ambiguous',
            'An earlier label purchase for this order has an unknown outcome — verify with the '
            'provider before buying again.',
        )
    if open_attempts.exists():
        return PurchaseOutcome('failed', 'transient', 'Another label purchase for this order is still in progress.')

    weight_oz, dimensions = get_order_shipping_config(order)
    if not weight_oz:
        message = 'Order weight is unknown — match its products or set a weight override.'
        log_event(order, 'label_failed', description=message)
        return PurchaseOutcome('failed', 'data', message)

    best = shop_rates_for_order(order, weight_oz, dimensions)
    if not best:
        if not order.store.ship_from_configured:
            message = (f'No ship-from address configured for store "{order.store.name}" — '
                       'sync it from ShipStation before this order can be labeled.')
        else:
            message = ('No shipping rate available from any active provider for this order '
                       '(check Shipping Options, provider connections and the address).')
        logger.warning('%s (order %s)', message, order.id)
        log_event(order, 'label_failed', description=message)
        return PurchaseOutcome('failed', 'data', message)

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
            label_data = result.get('labelData')  # base64 PDF/image
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
            # A successful create call has not been observed yet — extract defensively.
            tracking = result.get('tracking_number') or result.get('trackingNumber') or ''
            shipment_id = str(result.get('id') or result.get('shipment_id') or '')
            cost = result.get('total_cost') or result.get('cost') or best['cost']
            label = result.get('label')
            label_url = result.get('label_url') or (label.get('url') if isinstance(label, dict) else '') or ''
            label_data = label.get('data') if isinstance(label, dict) else None
        else:
            raise ValueError(f'Unknown provider {best["provider"]}')
        if not tracking:
            # The call returned but we cannot identify the label: it may well exist.
            raise LabelOutcomeUnknown(
                f'{account.get_provider_display()} returned no tracking number: {str(result)[:200]}'
            )
    except Exception as exc:
        kind = classify_label_error(exc)
        message = str(exc)[:500]
        attempt.error_message = message[:2000]
        if kind == 'ambiguous':
            attempt.status = LabelPurchaseAttempt.Status.NEEDS_REVIEW
            attempt.save(update_fields=['status', 'error_message'])
            log_event(
                order, 'label_failed',
                description=f'{account.get_provider_display()}: outcome unknown, will NOT retry automatically — {message}'[:280],
            )
            logger.error('Label purchase outcome UNKNOWN for order %s via %s: %s', order.id, best['provider'], exc)
            return PurchaseOutcome('needs_review', 'ambiguous', message)
        attempt.status = LabelPurchaseAttempt.Status.FAILED
        attempt.save(update_fields=['status', 'error_message'])
        logger.warning('Label purchase failed for order %s via %s: %s', order.id, best['provider'], exc)
        log_event(order, 'label_failed', description=f'{account.get_provider_display()}: {message}'[:280])
        return PurchaseOutcome('failed', kind, message)

    return _finalize_purchase(
        order, attempt, account, tracking=tracking, shipment_id=shipment_id, cost=cost,
        label_url=label_url, label_data=label_data, batch_order=batch_order,
    )


def reconcile_attempt(order, batch_order=None):
    """Find out what really happened to a purchase whose outcome was unknown.

    ByeLabel: the newest shipments are checked for one matching this attempt
    (created after it, same service, same destination ZIP). ShipStation: the order's
    non-voided shipments. Exactly one match -> the label is adopted (stored and
    confirmed); none, once the attempt is old enough to be sure -> the attempt is
    marked failed so the order can be bought again safely; anything else stays
    'needs_review' for a person.
    """
    from datetime import timedelta

    from django.utils import timezone as tz
    from django.utils.dateparse import parse_datetime

    from apps.orders.services import log_event

    from .byelabel_client import ByeLabelClient
    from .client import ShipStationClient
    from .models import LabelProviderAccount, LabelPurchaseAttempt

    attempt = (
        LabelPurchaseAttempt.objects.filter(
            order=order,
            status__in=(LabelPurchaseAttempt.Status.NEEDS_REVIEW, LabelPurchaseAttempt.Status.REQUESTED),
        ).select_related('provider_account').order_by('-created_at').first()
    )
    if not attempt or not attempt.provider_account:
        return PurchaseOutcome('failed', 'other', 'No unresolved purchase attempt to reconcile.')

    account = attempt.provider_account
    age = tz.now() - attempt.created_at
    zip5 = (order.ship_to_postal_code or '')[:5]

    try:
        if account.provider == LabelProviderAccount.Provider.BYELABEL:
            client = ByeLabelClient(account)
            matches = []
            for s in client.list_shipments(limit=50):
                created = parse_datetime(s.get('created_at') or '')
                carrier_service = ((s.get('carrier') or {}).get('service') or {}).get('code')
                to_zip = ((s.get('to') or {}).get('zip_code') or '')[:5]
                if (created and created >= attempt.created_at - timedelta(minutes=1)
                        and carrier_service == attempt.service_code and to_zip == zip5
                        and (s.get('status') or '') != 'void'):
                    matches.append(s)
            if len(matches) == 1:
                full = client.get_shipment(matches[0]['id'])
                label = full.get('label') or {}
                outcome = _finalize_purchase(
                    order, attempt, account,
                    tracking=full.get('tracking_number') or '', shipment_id=str(full.get('id') or ''),
                    cost=full.get('total_cost') or full.get('cost') or attempt.cost,
                    label_url='', label_data=label.get('data'), batch_order=batch_order,
                )
                log_event(order, 'label_confirmed', description='Recovered from ByeLabel after an unknown outcome')
                return outcome
            found = len(matches)
        else:
            client = ShipStationClient(account.shipstation_account)
            shipment = client.get_order_shipments(order.shipstation_order_id)
            if shipment:
                outcome = _finalize_purchase(
                    order, attempt, account,
                    tracking=shipment.get('trackingNumber') or '', shipment_id=str(shipment.get('shipmentId') or ''),
                    cost=shipment.get('shipmentCost') or attempt.cost, label_url='',
                    label_data=shipment.get('labelData'), batch_order=batch_order,
                )
                log_event(order, 'label_confirmed', description='Recovered from ShipStation after an unknown outcome')
                return outcome
            found = 0
    except Exception as exc:
        return PurchaseOutcome('needs_review', 'ambiguous', f'Could not check with the provider: {exc}'[:300])

    if found == 0 and age >= timedelta(minutes=3):
        attempt.status = LabelPurchaseAttempt.Status.FAILED
        attempt.error_message = 'Checked with the provider: no label was created for this attempt.'
        attempt.save(update_fields=['status', 'error_message'])
        log_event(order, 'label_failed', description='Verified with the provider: no label exists — safe to retry')
        return PurchaseOutcome('failed', 'transient', attempt.error_message)
    if found == 0:
        return PurchaseOutcome(
            'needs_review', 'ambiguous',
            'Too soon to be sure the label was not created — check again in a few minutes.',
        )
    return PurchaseOutcome(
        'needs_review', 'ambiguous',
        f'{found} possible matching labels found at the provider — check which one is this order.',
    )


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
