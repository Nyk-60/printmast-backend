"""ByeLabel API client — rate shopping and label purchase.

Auth, get_carriers() and get_rates() are verified against the live API
(doc.byelabel.com + direct probing). create_label()'s request shape is
mapped as far as validation errors allow WITHOUT ever completing a real
purchase — the exact response shape (label file field, tracking number
key, cost key) is UNVERIFIED and must be confirmed with one real, PC
-approved test purchase before this is relied on in production.
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class ByeLabelError(Exception):
    def __init__(self, message, status_code=None, code=''):
        super().__init__(message)
        self.status_code = status_code
        self.code = code  # ByeLabel's own error code, e.g. VALIDATION_ERROR


def _session_with_retries():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    s.mount('https://', HTTPAdapter(max_retries=retry))
    return s


class ByeLabelClient:
    BASE = 'https://api.byelabel.com/v1'

    def __init__(self, account):
        """account: LabelProviderAccount with provider == BYELABEL."""
        self._account = account
        self._session = _session_with_retries()
        self._token = None

    def _url(self, path):
        return f'{self.BASE}/{path.lstrip("/")}'

    @staticmethod
    def _parse(r):
        try:
            data = r.json()
        except ValueError:
            raise ByeLabelError(f'Non-JSON response ({r.status_code}): {r.text[:200]}', r.status_code)
        if not data.get('success'):
            err = data.get('error') or {}
            raise ByeLabelError(err.get('message', 'Unknown ByeLabel error'), r.status_code, err.get('code', ''))
        return data

    def _login(self):
        r = self._session.post(
            self._url('/login'),
            json={
                'client_id': self._account.byelabel_client_id,
                'client_secret': self._account.byelabel_client_secret,
            },
            headers={'X-Workspace-Id': self._account.byelabel_workspace_id},
            timeout=15,
        )
        data = self._parse(r)
        self._token = data['payload']['access']['token']

    def _request(self, method, path, json=None, _retried=False):
        if not self._token:
            self._login()
        r = self._session.request(
            method, self._url(path), json=json, timeout=30,
            headers={
                'Authorization': f'Bearer {self._token}',
                'X-Workspace-Id': self._account.byelabel_workspace_id,
            },
        )
        if r.status_code == 401 and not _retried:
            self._token = None
            return self._request(method, path, json=json, _retried=True)
        return self._parse(r)['payload']

    def verify(self):
        """Quick connectivity check — returns (True, None) or (False, error_message)."""
        try:
            self._login()
            return True, None
        except ByeLabelError as e:
            return False, str(e)
        except requests.exceptions.RequestException as e:
            return False, f'Network error: {e}'

    # ── Account / shipments (read-only) ─────────────────────────────────────

    def get_balance(self):
        """[{currency, amount}] — the workspace's prepaid balance."""
        return self._request('GET', '/balance')

    def list_shipments(self, limit=50):
        """Newest-first list of shipments (filters are not supported by the API).
        Used to check whether a label purchase that timed out actually went through."""
        return (self._request('GET', f'/shipment?limit={int(limit)}') or {}).get('data', [])

    def get_shipment(self, shipment_id):
        """Full shipment including label{format, data(base64)}."""
        return self._request('GET', f'/shipment/{shipment_id}')

    # ── Carriers ─────────────────────────────────────────────────────────────

    def get_carriers(self):
        """Returns list of {id, type, code, name, service:[...], packaging:[...]}."""
        return self._request('GET', '/carriers')

    # ── Rates ────────────────────────────────────────────────────────────────

    @staticmethod
    def _package(weight_oz, dimensions):
        """package[0] with weight and — when known — dimensions. The key is
        `dimensions` (plural): the singular `dimension` is rejected, and dimensions
        change the price materially (dimensional weight), so quoting without them
        would make ByeLabel look cheaper than it is next to ShipStation."""
        weight_lb = max(float(weight_oz), 0.1) / 16.0
        package = {'weight': {'value': round(weight_lb, 3), 'unit': 'lb'}}
        if dimensions and all(dimensions.get(k) for k in ('length', 'width', 'height')):
            package['dimensions'] = {
                'unit': 'in',
                'length': dimensions['length'], 'width': dimensions['width'], 'height': dimensions['height'],
            }
        return package

    def get_rates(self, *, from_postal, to_postal, to_country, weight_oz, carrier_code=None, dimensions=None):
        """Normalized to the same shape as ShipStationClient.get_rates() so callers
        can treat providers interchangeably: [{carrierCode, serviceCode, serviceName, shipmentCost, otherCost}]."""
        body = {
            'from': {'zip_code': from_postal, 'country_code': 'US'},
            'to': {'zip_code': to_postal, 'country_code': to_country or 'US', 'residential': True},
            'package': [self._package(weight_oz, dimensions)],
        }
        try:
            results = self._request('POST', '/rates', json=body)
        except ByeLabelError:
            return []  # no rates available for this route/account — skip, don't break rate-shopping

        rates = []
        for r in results or []:
            carrier = r.get('carrier') or {}
            service = carrier.get('service') or {}
            rate = r.get('rate') or {}
            code = carrier.get('code')
            if carrier_code and code != carrier_code:
                continue
            rates.append({
                'carrierCode': code,
                'serviceCode': service.get('code'),
                'serviceName': service.get('name'),
                'shipmentCost': rate.get('cost'),
                'otherCost': 0,
            })
        return rates

    # ── Labels ───────────────────────────────────────────────────────────────

    def create_label(self, *, carrier_code, service_code, packaging_code, ship_from, ship_to,
                     weight_oz, dimensions=None, items=None):
        """Purchase a label. ship_from/ship_to: dicts with name, phone, street1,
        street2, city, state, zip_code, country (+ optional company, email).
        items: [{description, quantity, code, total_price}] — REQUIRED by the API
        even for domestic shipments.

        The request schema was mapped by probing with a deliberately invalid
        carrier (so no purchase could happen): unknown keys are rejected, `street`
        must be a list, `items` is required and priced via `total_selling_price`.
        The success RESPONSE is modelled on GET /shipment/{id} (id, status,
        tracking_number, cost, label{format,data(base64 pdf)}) but no create call
        has succeeded yet, so it remains unverified until a live order.
        """
        def _address(a, residential):
            out = {
                'name': a.get('name') or '',
                'phone': a.get('phone') or '0000000000',
                'street': [s for s in (a.get('street1'), a.get('street2')) if s],
                'city': a.get('city') or '',
                'state_code': a.get('state') or '',
                'zip_code': a.get('zip_code') or '',
                'country_code': a.get('country') or 'US',
                'residential': residential,
            }
            for optional in ('company', 'email'):
                if a.get(optional):
                    out[optional] = a[optional]
            return out

        def _item(i):
            out = {
                'description': (i.get('description') or 'Apparel')[:50],
                'quantity': int(i.get('quantity') or 1),
                'country_of_origin': 'US',
                'total_selling_price': float(i.get('total_price') or 0),
            }
            if i.get('code'):  # empty strings are rejected by this API's validator
                out['code'] = i['code'][:30]
            return out

        body = {
            'carrier': {'code': carrier_code, 'service': service_code, 'packaging': packaging_code},
            'from': _address(ship_from, False),
            'to': _address(ship_to, True),
            'package': [self._package(weight_oz, dimensions)],
            'items': [_item(i) for i in (items or [{'description': 'Apparel', 'quantity': 1}])],
        }
        return self._request('POST', '/create-label', json=body)
