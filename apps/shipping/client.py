"""ShipStation API client supporting V1 (Basic Auth) and V2 (API-Key header)."""
import requests
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
        """Quick connectivity check — returns True/False."""
        try:
            if self._account.api_version == 'V1':
                self._get('/stores')
            else:
                self._get('/accounts/stores')
            return True
        except (ShipStationError, requests.exceptions.RequestException):
            return False

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

    def get_orders(self, store_id, page=1, page_size=500, modified_after=None):
        """Fetch orders for a given store. Returns (orders_list, total_pages)."""
        if self._account.api_version == 'V1':
            params = {
                'storeId': store_id,
                'orderStatus': 'awaiting_shipment',
                'pageSize': page_size,
                'page': page,
            }
            if modified_after:
                params['modifyDateStart'] = modified_after.strftime('%Y-%m-%d %H:%M:%S')
            data = self._get('/orders', params=params)
            return data.get('orders', []), data.get('pages', 1)
        else:
            params = {
                'storeId': store_id,
                'pageSize': page_size,
                'page': page,
                'sortBy': 'orderDate',
                'sortDir': 'DESC',
            }
            data = self._get('/orders', params=params)
            return data.get('orders', []), data.get('total', 0) // page_size + 1

    def get_order(self, shipstation_order_id):
        """Fetch a single order by ShipStation order ID."""
        if self._account.api_version == 'V1':
            return self._get(f'/orders/{shipstation_order_id}')
        else:
            return self._get(f'/orders/{shipstation_order_id}')

    # ── Rates ────────────────────────────────────────────────────────────────

    def get_rates(self, *, from_postal, to_postal, to_country, weight_oz,
                  carrier_code=None, dimensions=None):
        """
        Returns list of rate dicts: [{carrierCode, serviceCode, serviceName, shipmentCost, otherCost}]
        """
        payload = {
            'carrierCode': carrier_code,
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
            payload['dimensions'] = dimensions  # {length, width, height, units}

        if self._account.api_version == 'V1':
            return self._post('/shipments/getrates', json=payload)
        else:
            return self._post('/rates', json=payload).get('rates', [])

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
