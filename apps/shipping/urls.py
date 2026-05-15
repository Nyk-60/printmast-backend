from django.urls import path

from .views import (
    AccountDetailView,
    AccountListCreateView,
    AccountSyncView,
    RatesView,
    ShipStationWebhookView,
    StoreAssignView,
    StoreListView,
)

urlpatterns = [
    # ShipStation account management (print center)
    path('accounts/', AccountListCreateView.as_view(), name='ss-account-list'),
    path('accounts/<int:pk>/', AccountDetailView.as_view(), name='ss-account-detail'),
    path('accounts/<int:pk>/sync/', AccountSyncView.as_view(), name='ss-account-sync'),

    # Store management (print center)
    path('stores/', StoreListView.as_view(), name='store-list'),
    path('stores/<int:pk>/assign/', StoreAssignView.as_view(), name='store-assign'),

    # Rates (sub-user)
    path('rates/', RatesView.as_view(), name='shipping-rates'),

    # Webhook receiver (ShipStation calls this when orders arrive)
    path('webhook/<int:account_id>/', ShipStationWebhookView.as_view(), name='ss-webhook'),
]
