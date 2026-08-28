"""Celery tasks for ShipStation sync."""
from celery import shared_task
from django.utils import timezone


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_stores_for_account(self, account_id):
    """Pull stores from ShipStation and upsert into DB."""
    from .client import ShipStationClient, ShipStationError
    from .models import ShipStationAccount, Store

    try:
        account = ShipStationAccount.objects.get(pk=account_id, is_active=True)
    except ShipStationAccount.DoesNotExist:
        return

    client = ShipStationClient(account)
    try:
        stores = client.get_stores()
    except ShipStationError as exc:
        raise self.retry(exc=exc)

    for s in stores:
        store_id = str(s.get('storeId') or s.get('store_id', ''))
        name = s.get('storeName') or s.get('name', '')
        marketplace = s.get('marketplaceName') or s.get('marketplace', '')
        website = s.get('websiteUrl') or s.get('website', '') or ''
        Store.objects.update_or_create(
            shipstation_account=account,
            shipstation_store_id=store_id,
            defaults={'name': name, 'marketplace': marketplace, 'marketplace_url': website},
        )

    account.last_synced_at = timezone.now()
    account.save(update_fields=['last_synced_at'])


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def sync_orders_for_store(self, store_id, modified_after_iso=None, update_only=False):
    """Pull new/updated orders from ShipStation for a store.

    modified_after_iso: ISO datetime string — fetch only orders changed after this time.
    update_only: when True, only update status of orders that already exist in our DB.
                 Never creates new records. Used for status-check syncs.
    """
    from .client import ShipStationClient, ShipStationError
    from .models import Store

    try:
        store = Store.objects.select_related('shipstation_account').get(pk=store_id, is_active=True)
    except Store.DoesNotExist:
        return

    client = ShipStationClient(store.shipstation_account)
    modified_after = None
    if modified_after_iso:
        from datetime import datetime
        modified_after = datetime.fromisoformat(modified_after_iso)

    if modified_after:
        # Status-refresh mode: single paginated pass, all statuses
        page, total_pages = 1, 1
        while page <= total_pages:
            try:
                orders, total_pages = client.get_orders(
                    store.shipstation_store_id, page=page, modified_after=modified_after
                )
            except ShipStationError as exc:
                raise self.retry(exc=exc)
            _upsert_orders(store, orders, update_only=update_only, client=client)
            page += 1
    else:
        # Full import mode: fetch awaiting_shipment orders (new orders waiting for a label)
        all_ss_ids = []
        page, total_pages = 1, 1
        while page <= total_pages:
            try:
                orders, total_pages = client.get_orders(
                    store.shipstation_store_id, page=page,
                    order_status='awaiting_shipment',
                )
            except ShipStationError as exc:
                raise self.retry(exc=exc)
            _upsert_orders(store, orders, update_only=False, client=client)
            all_ss_ids.extend(str(o.get('orderId', '')) for o in orders)
            page += 1

        # Hide DB orders with unprocessed statuses that are no longer open in ShipStation
        if all_ss_ids:
            from apps.orders.models import Order
            (
                Order.objects
                .filter(store=store, is_hidden=False, status__in=('new', 'matching'))
                .exclude(shipstation_order_id__in=all_ss_ids)
                .update(is_hidden=True)
            )


# ShipStation terminal statuses → our status (only override to these, never downgrade)
_SS_TO_STATUS = {
    'shipped':   'shipped',
    'cancelled': 'cancelled',
}


def _upsert_orders(store, raw_orders, update_only=False, client=None):
    """Create or update Order + OrderItem records from raw ShipStation data.

    update_only=True: only update status/tracking on orders already in our DB.
    client: ShipStationClient — used to fetch accurate tracking via /shipments.
    """
    from django.utils.dateparse import parse_datetime

    from apps.orders.models import Order, OrderItem

    sub_user = store.assigned_sub_users.filter(is_active=True).first()
    if not sub_user:
        return  # store not assigned — skip

    for raw in raw_orders:
        ss_order_id = str(raw.get('orderId', ''))
        if not ss_order_id:
            continue

        if update_only:
            # Status-refresh mode: only touch orders we already know about.
            try:
                order = Order.objects.get(store=store, shipstation_order_id=ss_order_id)
            except Order.DoesNotExist:
                continue
            created = False
        else:
            ship_to = raw.get('shipTo') or {}
            defaults = {
                'sub_user': sub_user,
                'shipstation_order_number': raw.get('orderNumber') or '',
                'external_order_number': raw.get('orderNumber') or '',
                'customer_name': raw.get('customerName') or raw.get('customerUsername') or '',
                'customer_email': raw.get('customerEmail') or '',
                'ship_to_name': ship_to.get('name') or '',
                'ship_to_company': ship_to.get('company') or '',
                'ship_to_address1': ship_to.get('street1') or '',
                'ship_to_address2': ship_to.get('street2') or '',
                'ship_to_city': ship_to.get('city') or '',
                'ship_to_state': ship_to.get('state') or '',
                'ship_to_postal_code': ship_to.get('postalCode') or '',
                'ship_to_country': ship_to.get('country') or 'US',
                'requested_service': raw.get('requestedShippingService') or '',
                'buyer_note':    raw.get('customerNotes') or '',
                'gift_message':  raw.get('giftMessage') or '',
                'is_gift':       bool(raw.get('gift')),
                'internal_note': raw.get('internalNotes') or '',
                'ordered_at': parse_datetime(raw['orderDate']) if raw.get('orderDate') else None,
            }
            order, created = Order.objects.update_or_create(
                store=store,
                shipstation_order_id=ss_order_id,
                defaults=defaults,
            )

        # Sync terminal status from ShipStation (shipped / cancelled)
        ss_status = (raw.get('orderStatus') or '').lower()
        new_status = _SS_TO_STATUS.get(ss_status)

        # Don't import orders that arrived already completed — they were
        # fulfilled outside this platform. Only update existing ones.
        if created and new_status:
            order.delete()
            continue

        if new_status and order.status != new_status:
            update_fields = ['status']
            order.status = new_status
            # Try order-level trackingNumber first; if missing, call /shipments
            tracking = (
                raw.get('trackingNumber')
                or (raw.get('shipments') or [{}])[0].get('trackingNumber')
                or ''
            )
            if not tracking and client:
                try:
                    shipment = client.get_order_shipments(order.shipstation_order_id)
                    if shipment:
                        tracking = shipment.get('trackingNumber') or ''
                except Exception:
                    pass
            if tracking and not order.tracking_number:
                order.tracking_number = tracking
                update_fields.append('tracking_number')
            order.save(update_fields=update_fields)

        if created:
            for item in raw.get('items') or []:
                options = {o['name']: o['value'] for o in item.get('options') or []}
                OrderItem.objects.create(
                    order=order,
                    shipstation_item_id=str(item.get('orderItemId') or ''),
                    marketplace_sku=item.get('sku') or '',
                    raw_product_name=item.get('name') or '',
                    raw_options=options,
                    personalization=options.get('Personalization') or '',
                    image_url=item.get('imageUrl') or '',
                    quantity=item.get('quantity') or 1,
                    unit_price=item.get('unitPrice') or 0,
                )

            if sub_user.auto_product_matching:
                from apps.orders.services import auto_match_order, order_is_matched
                auto_match_order(order)
                if order_is_matched(order):
                    try:
                        push_order_shipping_to_shipstation(order.pk)
                    except Exception:
                        pass
                    try:
                        refresh_shipping_rate_for_order(order.pk)
                    except Exception:
                        pass


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def push_personalization_to_shipstation(self, order_id, item_id):
    """Push a platform-side personalization edit back to ShipStation."""
    from apps.orders.models import Order, OrderItem
    from .client import ShipStationClient, ShipStationError

    try:
        order = Order.objects.select_related('store__shipstation_account').prefetch_related('items').get(pk=order_id)
        item  = order.items.get(pk=item_id)
    except (Order.DoesNotExist, OrderItem.DoesNotExist):
        return

    client = ShipStationClient(order.store.shipstation_account)
    try:
        client.update_item_personalization(order, item)
    except ShipStationError as exc:
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def push_address_to_shipstation(self, order_id):
    """Push a platform-side address edit back to ShipStation."""
    from apps.orders.models import Order
    from .client import ShipStationClient, ShipStationError

    try:
        order = Order.objects.select_related('store__shipstation_account').prefetch_related('items').get(pk=order_id)
    except Order.DoesNotExist:
        return

    client = ShipStationClient(order.store.shipstation_account)
    try:
        client.update_order_address(order)
    except ShipStationError as exc:
        raise self.retry(exc=exc)


@shared_task
def sync_single_order_from_webhook(account_id, resource_url, resource_type):
    """
    Called immediately when ShipStation fires a webhook.
    Parses the resource_url to find the order ID and syncs just that order.
    Falls back to a short-window store sync if parsing fails.
    """
    import re
    from datetime import timedelta

    from django.utils import timezone

    from .client import ShipStationClient, ShipStationError
    from .models import ShipStationAccount, Store

    try:
        account = ShipStationAccount.objects.get(pk=account_id, is_active=True)
    except ShipStationAccount.DoesNotExist:
        return

    client = ShipStationClient(account)

    # ShipStation resource_url for ORDER_NOTIFY looks like:
    #   https://ssapi.shipstation.com/orders?...&orderId=123456789
    # SHIP_NOTIFY may reference shipments; extract orderId if present.
    order_id_match = re.search(r'orderId[=:](\d+)', resource_url)
    if order_id_match:
        ss_order_id = order_id_match.group(1)
        try:
            raw = client.get_order(ss_order_id)
        except ShipStationError:
            raw = None

        if raw:
            store_id = str(raw.get('advancedOptions', {}).get('storeId') or raw.get('storeId') or '')
            store = Store.objects.filter(
                shipstation_account=account,
                shipstation_store_id=store_id,
                is_active=True,
            ).first()
            if store:
                _upsert_orders(store, [raw], client=client)
                return

    # Fallback: sync all stores for this account with a short window
    modified_after = (timezone.now() - timedelta(minutes=30)).isoformat()
    for store in account.stores.filter(is_active=True):
        try:
            sync_orders_for_store.delay(store.pk, modified_after_iso=modified_after)
        except Exception:
            sync_orders_for_store(store.pk, modified_after_iso=modified_after)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def push_order_shipping_to_shipstation(self, order_id):
    """Push weight + dimensions to ShipStation after an order is fully matched.

    Uses shipping_override if the PC has set one; otherwise auto-calculates
    from matched pc_variant fields.
    """
    from apps.orders.models import Order
    from apps.shipping.utils import get_order_shipping_config
    from .client import ShipStationClient, ShipStationError

    try:
        order = (
            Order.objects
            .select_related('store__shipstation_account')
            .prefetch_related('items__pc_variant__dimension_rules')
            .get(pk=order_id)
        )
    except Order.DoesNotExist:
        return

    weight_oz, dimensions = get_order_shipping_config(order)

    client = ShipStationClient(order.store.shipstation_account)
    try:
        client.update_order_shipping_config(order, weight_oz=weight_oz, dimensions=dimensions)
    except ShipStationError as exc:
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def auto_select_shipping_for_order(self, order_id):
    """After an order is fully matched, auto-select a carrier/service + rate.

    Uses the order's requested_service to determine a category (standard/
    priority/express), then applies the sub-user's configured default for
    that category — constrained to the print center's offered options for
    that category. Falls back to the cheapest of the allowed options.

    Does nothing if the order already has a carrier/service selected, or if
    the print center hasn't configured any options for the category.
    """
    from apps.orders.models import Order
    from apps.shipping.models import ShippingServiceDefault, ShippingServiceOption
    from apps.shipping.utils import categorize_requested_service, get_order_shipping_config
    from .client import ShipStationClient, ShipStationError

    try:
        order = (
            Order.objects
            .select_related('store__shipstation_account', 'sub_user')
            .prefetch_related('items__pc_variant__dimension_rules')
            .get(pk=order_id)
        )
    except Order.DoesNotExist:
        return

    if order.selected_carrier_code:
        return

    pc = order.store.shipstation_account.print_center
    category = categorize_requested_service(order.requested_service)

    options = list(ShippingServiceOption.objects.filter(
        print_center=pc, category=category, is_active=True
    ))
    if not options:
        return  # PC hasn't configured this category — leave for manual selection

    weight_oz, dimensions = get_order_shipping_config(order)
    if not weight_oz:
        return

    client = ShipStationClient(order.store.shipstation_account)
    from_postal = pc.ship_from_postal_code or '00000'
    try:
        rates = client.get_rates(
            from_postal=from_postal,
            to_postal=order.ship_to_postal_code,
            to_country=order.ship_to_country or 'US',
            weight_oz=weight_oz,
            dimensions=dimensions,
        )
    except ShipStationError as exc:
        raise self.retry(exc=exc)

    # Cheapest rate per (carrier, service), restricted to the PC's options for this category
    allowed = {(o.carrier_code, o.service_code) for o in options}
    best = {}
    for r in rates:
        key = (r.get('carrierCode'), r.get('serviceCode'))
        if key not in allowed:
            continue
        cost = float(r.get('shipmentCost', 0)) + float(r.get('otherCost', 0))
        if key not in best or cost < best[key]:
            best[key] = cost

    if not best:
        return

    default = ShippingServiceDefault.objects.filter(
        sub_user=order.sub_user, category=category
    ).select_related('service_option').first()

    chosen_key = None
    if default and default.service_option:
        candidate = (default.service_option.carrier_code, default.service_option.service_code)
        if candidate in best:
            chosen_key = candidate
    if chosen_key is None:
        chosen_key = min(best, key=best.get)

    base_cost = best[chosen_key]

    sub_profile = order.sub_user
    markup_type = sub_profile.shipping_markup_type
    markup_value = float(sub_profile.shipping_markup_value)
    if markup_type == 'percent':
        displayed = base_cost * (1 + markup_value / 100)
    else:
        displayed = base_cost + markup_value

    order.selected_carrier_code = chosen_key[0]
    order.selected_service_code = chosen_key[1]
    order.shipping_cost_raw = round(base_cost, 2)
    order.shipping_cost = round(displayed, 2)
    order.save(update_fields=[
        'selected_carrier_code', 'selected_service_code', 'shipping_cost_raw', 'shipping_cost',
    ])


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def refresh_shipping_rate_for_order(self, order_id):
    """Recompute shipping_cost for the order's already-selected carrier/service
    using the current weight/dims — keeps the displayed price (with markup) in
    sync with what ShipStation would actually charge right now.

    If no carrier/service is selected yet, falls back to auto-selecting one.
    No-op if the print center hasn't configured this category, or weight is unknown.
    """
    from apps.orders.models import Order
    from apps.shipping.utils import get_order_shipping_config
    from .client import ShipStationClient, ShipStationError

    try:
        order = (
            Order.objects
            .select_related('store__shipstation_account', 'sub_user')
            .prefetch_related('items__pc_variant__dimension_rules')
            .get(pk=order_id)
        )
    except Order.DoesNotExist:
        return

    if not order.selected_carrier_code or not order.selected_service_code:
        auto_select_shipping_for_order(order_id)
        return

    weight_oz, dimensions = get_order_shipping_config(order)
    if not weight_oz:
        return

    pc = order.store.shipstation_account.print_center
    client = ShipStationClient(order.store.shipstation_account)
    from_postal = pc.ship_from_postal_code or '00000'
    try:
        rates = client.get_rates(
            from_postal=from_postal,
            to_postal=order.ship_to_postal_code,
            to_country=order.ship_to_country or 'US',
            weight_oz=weight_oz,
            dimensions=dimensions,
            carrier_code=order.selected_carrier_code,
        )
    except ShipStationError as exc:
        raise self.retry(exc=exc)

    match = next((r for r in rates if r.get('serviceCode') == order.selected_service_code), None)
    if not match:
        return

    base_cost = float(match.get('shipmentCost', 0)) + float(match.get('otherCost', 0))

    sub_profile = order.sub_user
    markup_type = sub_profile.shipping_markup_type
    markup_value = float(sub_profile.shipping_markup_value)
    if markup_type == 'percent':
        displayed = base_cost * (1 + markup_value / 100)
    else:
        displayed = base_cost + markup_value

    order.shipping_cost_raw = round(base_cost, 2)
    order.shipping_cost = round(displayed, 2)
    order.save(update_fields=['shipping_cost_raw', 'shipping_cost'])


@shared_task
def sync_all_active_accounts():
    """Periodic task: sync orders modified in the last 2 hours for all active stores."""
    from datetime import timedelta
    from django.utils import timezone
    from .models import Store

    # 2-hour window ensures we catch any status changes ShipStation webhooks may have missed
    modified_after = (timezone.now() - timedelta(hours=2)).isoformat()

    for store in Store.objects.filter(is_active=True, assigned_sub_users__isnull=False).distinct():
        sync_orders_for_store.delay(store.pk, modified_after_iso=modified_after)


@shared_task
def register_webhooks_for_account(account_id):
    """Register ORDER_NOTIFY and SHIP_NOTIFY webhooks with ShipStation."""
    from django.conf import settings
    from .client import ShipStationClient, ShipStationError
    from .models import ShipStationAccount

    try:
        account = ShipStationAccount.objects.get(pk=account_id, is_active=True)
    except ShipStationAccount.DoesNotExist:
        return

    site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
    if not site_url or 'localhost' in site_url:
        return  # webhooks require a public URL

    webhook_url = f'{site_url}/shipping/webhook/{account_id}/'
    client = ShipStationClient(account)
    # ORDER_NOTIFY: new/updated orders
    # SHIP_NOTIFY: label created / order shipped in ShipStation
    for event in ('ORDER_NOTIFY', 'SHIP_NOTIFY'):
        try:
            client.register_webhook(webhook_url, event)
        except ShipStationError:
            pass  # non-fatal — periodic sync is the fallback
