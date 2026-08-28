"""ShipStation API client supporting V1 (Basic Auth) and V2 (API-Key header)."""
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class ShipStationError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _session_with_retries():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    s.mount('https://', HTTPAdapter(max_retries=retry))
    s.verify = not settings.DEBUG  # False in dev (Windows SSL issues), True in production
    return s


class ShipStationClient:
    V1_BASE = 'https://ssapi.shipstation.com'
    V2_BASE = 'https://api.shipstation.com/v2'

    def __init__(self, account):
        """account: ShipStationAccount model instance"""
        self._account = account
        self._session = _session_with_retries()
        if account.api_version == 'V1':
            self._session.auth = (account.api_key, account.api_secret)
            self._base = self.V1_BASE
        else:
            self._session.headers.update({'API-Key': account.api_key})
            self._base = self.V2_BASE

    def _url(self, path):
        return f'{self._base}/{path.lstrip("/")}'

    def _get(self, path, params=None):
        r = self._session.get(self._url(path), params=params, timeout=30)
        if not r.ok:
            raise ShipStationError(r.text, r.status_code)
        return r.json()

    def _post(self, path, json=None):
        r = self._session.post(self._url(path), json=json, timeout=30)
        if not r.ok:
            raise ShipStationError(r.text, r.status_code)
        return r.json()

    def verify(self):
        """Quick connectivity check — returns (True, None) or (False, error_message)."""
        try:
            if self._account.api_version == 'V1':
                self._get('/stores')
            else:
                self._get('/accounts/stores')
            return True, None
        except ShipStationError as e:
            return False, f'ShipStation error {e.status_code}: {str(e)}'
        except requests.exceptions.RequestException as e:
            return False, f'Network error: {str(e)}'

    # ── Stores ───────────────────────────────────────────────────────────────

    def get_stores(self):
        """Returns list of store dicts from ShipStation."""
        if self._account.api_version == 'V1':
            data = self._get('/stores')
            return data if isinstance(data, list) else []
        else:
            data = self._get('/accounts/stores')
            return data.get('stores', [])

    # ── Orders ───────────────────────────────────────────────────────────────

    def get_orders(self, store_id, page=1, page_size=500, modified_after=None,
                   order_status='awaiting_shipment'):
        """Fetch orders for a given store. Returns (orders_list, total_pages).

        order_status: ShipStation status filter — used only when modified_after is None.
        When modified_after is given, ALL statuses are returned (status transitions).
        """
        if self._account.api_version == 'V1':
            params = {
                'storeId': store_id,
                'pageSize': page_size,
                'page': page,
            }
            if modified_after:
                params['modifyDateStart'] = modified_after.strftime('%Y-%m-%d %H:%M:%S')
            else:
                params['orderStatus'] = order_status
            data = self._get('/orders', params=params)
            return data.get('orders', []), data.get('pages', 1)
        else:
            params = {
                'storeId': store_id,
                'pageSize': page_size,
                'page': page,
                'sortBy': 'modifyDate',
                'sortDir': 'DESC',
            }
            if modified_after:
                params['modifyDateStart'] = modified_after.strftime('%Y-%m-%d %H:%M:%S')
            data = self._get('/orders', params=params)
            return data.get('orders', []), data.get('total', 0) // page_size + 1

    def get_order(self, shipstation_order_id):
        """Fetch a single order by ShipStation order ID."""
        if self._account.api_version == 'V1':
            return self._get(f'/orders/{shipstation_order_id}')
        else:
            return self._get(f'/orders/{shipstation_order_id}')

    def get_order_shipments(self, ss_order_id):
        """Return the most recent non-voided shipment for an order, or None.

        Used to reliably obtain the tracking number after an order ships —
        the order object's trackingNumber field is not always populated.
        """
        if self._account.api_version == 'V1':
            data = self._get('/shipments', params={'orderId': ss_order_id, 'pageSize': 25})
            shipments = data.get('shipments', [])
        else:
            data = self._get(f'/orders/{ss_order_id}/shipments')
            shipments = data.get('shipments', [])
        active = [s for s in shipments if not s.get('voided')]
        # Most recent first (ShipStation returns newest last in V1, sort by createDate)
        active.sort(key=lambda s: s.get('createDate') or '', reverse=True)
        return active[0] if active else None

    # ── Order update ─────────────────────────────────────────────────────────

    @staticmethod
    def build_ship_from(order):
        """Build shipFrom dict from PC address + Store sender name.

        Physical address  → PrintCenterProfile.ship_from_*
        Sender name       → Store.ship_from_name (falls back to PC company name)
        Returns None if PC has no address configured yet.
        """
        pc    = order.store.shipstation_account.print_center
        store = order.store
        if not pc.ship_from_postal_code:
            return None
        return {
            'name':       store.ship_from_name or pc.ship_from_name or pc.company_name,
            'company':    store.ship_from_company or '',
            'street1':    pc.ship_from_street1,
            'street2':    pc.ship_from_street2 or '',
            'city':       pc.ship_from_city,
            'state':      pc.ship_from_state,
            'postalCode': pc.ship_from_postal_code,
            'country':    pc.ship_from_country or 'US',
            'phone':      pc.ship_from_phone or '',
        }

    def update_order_address(self, order):
        """Push updated shipTo address to ShipStation for an existing order."""
        ship_to = {
            'name':       order.ship_to_name,
            'company':    order.ship_to_company,
            'street1':    order.ship_to_address1,
            'street2':    order.ship_to_address2,
            'city':       order.ship_to_city,
            'state':      order.ship_to_state,
            'postalCode': order.ship_to_postal_code,
            'country':    order.ship_to_country or 'US',
            'residential': True,
        }
        if self._account.api_version == 'V1':
            raw = self._get(f'/orders/{order.shipstation_order_id}')
            raw['shipTo'] = ship_to
            raw['billTo'] = raw.get('billTo') or {'name': order.ship_to_name}
            ship_from = self.build_ship_from(order)
            if ship_from:
                raw['shipFrom'] = ship_from
            # ShipStation sometimes returns an empty items list for marketplace
            # orders after an API update. Rebuild from our DB to avoid clearing.
            db_items = list(order.items.all())
            if db_items and not raw.get('items'):
                raw['items'] = [
                    {
                        'orderItemId': int(i.shipstation_item_id) if i.shipstation_item_id else None,
                        'sku':         i.marketplace_sku or '',
                        'name':        i.raw_product_name or '',
                        'imageUrl':    i.image_url or '',
                        'quantity':    i.quantity,
                        'unitPrice':   float(i.unit_price),
                        'options':     [{'name': k, 'value': v} for k, v in (i.raw_options or {}).items()],
                        'adjustment':  float(i.unit_price) < 0,
                    }
                    for i in db_items
                ]
            return self._post('/orders/createorder', json=raw)
        else:
            return self._post(
                f'/orders/{order.shipstation_order_id}',
                json={'shipTo': ship_to},
            )

    def update_item_personalization(self, order, item):
        """Push a single item's personalization change to ShipStation.

        Fetches the full order first, updates only the target item's
        'Personalization' option, then posts the whole order back.
        This prevents any other fields from being cleared.
        """
        if self._account.api_version == 'V1':
            raw = self._get(f'/orders/{order.shipstation_order_id}')
            raw['shipTo'] = raw.get('shipTo') or {}
            raw['billTo'] = raw.get('billTo') or {}

            ss_items = raw.get('items') or []
            for ss_item in ss_items:
                if str(ss_item.get('orderItemId', '')) == str(item.shipstation_item_id):
                    options = ss_item.get('options') or []
                    # Remove existing Personalization option, add updated one
                    options = [o for o in options if o.get('name', '').lower() != 'personalization']
                    if item.personalization:
                        options.append({'name': 'Personalization', 'value': item.personalization})
                    ss_item['options'] = options
                    break

            # Rebuild items from DB if ShipStation returned empty list
            if not ss_items:
                db_items = list(order.items.all())
                raw['items'] = [
                    {
                        'orderItemId': int(i.shipstation_item_id) if i.shipstation_item_id else None,
                        'sku':         i.marketplace_sku or '',
                        'name':        i.raw_product_name or '',
                        'imageUrl':    i.image_url or '',
                        'quantity':    i.quantity,
                        'unitPrice':   float(i.unit_price),
                        'options':     [{'name': k, 'value': v} for k, v in (i.raw_options or {}).items()],
                        'adjustment':  float(i.unit_price) < 0,
                    }
                    for i in db_items
                ]

            return self._post('/orders/createorder', json=raw)
        else:
            # V2: partial update not supported for item options via simple patch;
            # fall back to no-op (personalization lives on our platform only)
            return None

    def update_order_shipping_config(self, order, *, weight_oz, dimensions=None):
        """Push weight (and optionally dimensions) to ShipStation for an order.

        dimensions: dict with keys length, width, height (inches) or None to leave unchanged.
        """
        if self._account.api_version == 'V1':
            raw = self._get(f'/orders/{order.shipstation_order_id}')
            raw.setdefault('shipTo', {})
            raw.setdefault('billTo', {})

            ship_from = self.build_ship_from(order)
            if ship_from:
                raw['shipFrom'] = ship_from

            if weight_oz:
                raw['weight'] = {'value': round(float(weight_oz), 2), 'units': 'ounces'}
            if dimensions:
                raw['dimensions'] = {
                    'units':  'inches',
                    'length': round(float(dimensions['length']), 2),
                    'width':  round(float(dimensions['width']),  2),
                    'height': round(float(dimensions['height']), 2),
                }

            db_items = list(order.items.all())
            if db_items and not raw.get('items'):
                raw['items'] = [
                    {
                        'orderItemId': int(i.shipstation_item_id) if i.shipstation_item_id else None,
                        'sku':         i.marketplace_sku or '',
                        'name':        i.raw_product_name or '',
                        'imageUrl':    i.image_url or '',
                        'quantity':    i.quantity,
                        'unitPrice':   float(i.unit_price),
                        'options':     [{'name': k, 'value': v} for k, v in (i.raw_options or {}).items()],
                        'adjustment':  float(i.unit_price) < 0,
                    }
                    for i in db_items
                ]
            return self._post('/orders/createorder', json=raw)
        else:
            payload = {}
            if weight_oz:
                payload['weight'] = {'value': round(float(weight_oz), 2), 'units': 'ounces'}
            if dimensions:
                payload['dimensions'] = {
                    'units':  'inches',
                    'length': round(float(dimensions['length']), 2),
                    'width':  round(float(dimensions['width']),  2),
                    'height': round(float(dimensions['height']), 2),
                }
            if payload:
                return self._post(f'/orders/{order.shipstation_order_id}', json=payload)

    # ── Rates ────────────────────────────────────────────────────────────────

    def get_carriers(self):
        """Return list of carriers connected to this account."""
        if self._account.api_version == 'V1':
            return self._get('/carriers')
        return self._get('/carriers').get('carriers', [])

    def get_rates(self, *, from_postal, to_postal, to_country, weight_oz,
                  carrier_code=None, dimensions=None):
        """
        Returns list of rate dicts: [{carrierCode, serviceCode, serviceName, shipmentCost, otherCost}]
        V1: carrierCode is required — if not supplied, fetches rates for all carriers.
        """
        base_payload = {
            'fromPostalCode': from_postal,
            'toState': '',
            'toCountry': to_country,
            'toPostalCode': to_postal,
            'toCity': '',
            'weight': {'value': float(weight_oz), 'units': 'ounces'},
            'confirmation': 'none',
            'residential': True,
        }
        if dimensions:
            base_payload['dimensions'] = dimensions

        if self._account.api_version == 'V2':
            if carrier_code:
                base_payload['carrierCode'] = carrier_code
            return self._post('/rates', json=base_payload).get('rates', [])

        # V1: quote box/package rates, not envelope variants
        base_payload['packageCode'] = 'package'

        # V1: carrierCode is required — iterate all carriers when none specified
        if carrier_code:
            rates = self._post('/shipments/getrates', json={**base_payload, 'carrierCode': carrier_code})
            for r in rates if isinstance(rates, list) else []:
                r.setdefault('carrierCode', carrier_code)
            return rates

        carriers = self.get_carriers()
        codes = [c.get('code') or c.get('carrierCode') for c in carriers]
        codes = [code for code in codes if code]

        def _fetch(code):
            try:
                rates = self._post('/shipments/getrates', json={**base_payload, 'carrierCode': code})
                for r in rates if isinstance(rates, list) else []:
                    r.setdefault('carrierCode', code)
                return rates if isinstance(rates, list) else []
            except ShipStationError:
                return []  # skip carriers that don't support this route

        all_rates = []
        with ThreadPoolExecutor(max_workers=max(1, len(codes))) as executor:
            for rates in executor.map(_fetch, codes):
                all_rates.extend(rates)
        return all_rates

    # ── Labels ───────────────────────────────────────────────────────────────

    def create_label(self, *, shipstation_order_id, carrier_code, service_code,
                     confirmation='none', test_label=False):
        """
        Purchase a label for an order.
        Returns dict with: shipmentId, labelData (PDF base64), trackingNumber, shipmentCost
        """
        payload = {
            'orderId': int(shipstation_order_id),
            'carrierCode': carrier_code,
            'serviceCode': service_code,
            'confirmation': confirmation,
            'shipDate': None,
            'weight': None,
            'dimensions': None,
            'insuranceOptions': None,
            'internationalOptions': None,
            'advancedOptions': None,
            'testLabel': test_label,
        }
        if self._account.api_version == 'V1':
            return self._post('/orders/createlabelfororder', json=payload)
        else:
            return self._post(f'/orders/{shipstation_order_id}/labels', json={
                'carrierCode': carrier_code,
                'serviceCode': service_code,
                'confirmation': confirmation,
                'testLabel': test_label,
            })

    # ── Webhooks ─────────────────────────────────────────────────────────────

    def register_webhook(self, target_url, event='ORDER_NOTIFY'):
        """Register a webhook for new orders. Returns hook dict."""
        if self._account.api_version == 'V1':
            return self._post('/webhooks/subscribe', json={
                'target_url': target_url,
                'event': event,
                'store_id': None,
                'friendly_name': f'Printmast — {self._account.label}',
            })
        else:
            return self._post('/webhooks', json={
                'url': target_url,
                'event': event,
                'name': f'Printmast — {self._account.label}',
            })
