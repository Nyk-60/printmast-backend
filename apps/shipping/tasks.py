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
def sync_orders_for_store(self, store_id, modified_after_iso=None):
    """Pull new/updated orders from ShipStation for a store."""
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

    page, total_pages = 1, 1
    while page <= total_pages:
        try:
            orders, total_pages = client.get_orders(
                store.shipstation_store_id, page=page, modified_after=modified_after
            )
        except ShipStationError as exc:
            raise self.retry(exc=exc)

        _upsert_orders(store, orders)
        page += 1


def _upsert_orders(store, raw_orders):
    """Create or update Order + OrderItem records from raw ShipStation data."""
    from django.utils.dateparse import parse_datetime

    from apps.orders.models import Order, OrderItem

    # Find sub-user assigned to this store (take first assigned, if any)
    sub_user = store.assigned_sub_users.filter(is_active=True).first()
    if not sub_user:
        return  # store not assigned — skip

    for raw in raw_orders:
        ss_order_id = str(raw.get('orderId', ''))
        if not ss_order_id:
            continue

        ship_to = raw.get('shipTo', {})
        defaults = {
            'sub_user': sub_user,
            'shipstation_order_number': raw.get('orderNumber', ''),
            'external_order_number': raw.get('orderNumber', ''),
            'customer_name': raw.get('customerUsername') or raw.get('customerEmail', ''),
            'customer_email': raw.get('customerEmail', ''),
            'ship_to_name': ship_to.get('name', ''),
            'ship_to_company': ship_to.get('company', ''),
            'ship_to_address1': ship_to.get('street1', ''),
            'ship_to_address2': ship_to.get('street2', ''),
            'ship_to_city': ship_to.get('city', ''),
            'ship_to_state': ship_to.get('state', ''),
            'ship_to_postal_code': ship_to.get('postalCode', ''),
            'ship_to_country': ship_to.get('country', 'US'),
            'requested_service': raw.get('requestedShippingService', ''),
            'buyer_note': raw.get('customerNotes', ''),
            'ordered_at': parse_datetime(raw['orderDate']) if raw.get('orderDate') else None,
        }

        order, created = Order.objects.update_or_create(
            store=store,
            shipstation_order_id=ss_order_id,
            defaults=defaults,
        )

        if created:
            for item in raw.get('items', []):
                options = {o['name']: o['value'] for o in item.get('options', [])}
                OrderItem.objects.create(
                    order=order,
                    shipstation_item_id=str(item.get('orderItemId', '')),
                    marketplace_sku=item.get('sku', ''),
                    raw_product_name=item.get('name', ''),
                    raw_options=options,
                    personalization=options.get('Personalization', ''),
                    image_url=item.get('imageUrl', ''),
                    quantity=item.get('quantity', 1),
                    unit_price=item.get('unitPrice', 0),
                )


@shared_task
def sync_all_active_accounts():
    """Periodic task: kick off sync for every active ShipStation account."""
    from .models import ShipStationAccount, Store

    for account in ShipStationAccount.objects.filter(is_active=True):
        sync_stores_for_account.delay(account.pk)

    for store in Store.objects.filter(is_active=True, assigned_sub_users__isnull=False).distinct():
        sync_orders_for_store.delay(store.pk)
