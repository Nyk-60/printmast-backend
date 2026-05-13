"""ShipStation API V2 client wrapper.

Marks the seam between Printmast and ShipStation. Real HTTP calls go here
so views stay thin and tests can mock this module.
"""
from __future__ import annotations

import requests
from django.conf import settings

from .models import ShippingRateMarkup


class ShipStationClient:
    def __init__(self):
        self.base_url = settings.SHIPSTATION_BASE_URL.rstrip('/')
        self.api_key = settings.SHIPSTATION_API_KEY
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({'API-Key': self.api_key})

    def _request(self, method, path, **kwargs):
        url = f'{self.base_url}{path}'
        response = self.session.request(method, url, timeout=30, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def list_orders(self, store_id=None, params=None):
        params = dict(params or {})
        if store_id:
            params['store_id'] = store_id
        return self._request('GET', '/v2/orders', params=params)

    def estimate_rates(self, payload):
        return self._request('POST', '/v2/rates/estimate', json=payload)

    def create_label(self, payload):
        return self._request('POST', '/v2/labels', json=payload)

    def update_order_address(self, order_id, address):
        return self._request('PATCH', f'/v2/orders/{order_id}', json={'ship_to': address})


def apply_markup(service_code, base_rate):
    try:
        markup = ShippingRateMarkup.objects.get(service=service_code)
    except ShippingRateMarkup.DoesNotExist:
        return base_rate
    return markup.apply(base_rate)
