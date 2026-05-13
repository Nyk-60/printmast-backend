from django.urls import path

from .views import RatesView, StoresView

urlpatterns = [
    path('rates/', RatesView.as_view(), name='shipping-rates'),
    path('stores/', StoresView.as_view(), name='shipping-stores'),
]
