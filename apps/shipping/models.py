from django.db import models

from apps.core.models import TimeStampedModel


class ShippingRateMarkup(TimeStampedModel):
    """Admin-defined markup applied on top of ShipStation rates."""
    service = models.CharField(max_length=20, unique=True, help_text='standard | priority | express')
    markup_flat = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    markup_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    def apply(self, base_rate):
        return (base_rate or 0) + float(self.markup_flat) + (float(base_rate or 0) * float(self.markup_percent) / 100)


class ShipStationCarrier(TimeStampedModel):
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
