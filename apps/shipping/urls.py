from django.urls import path

from .views import (
    AccountDetailView,
    AccountListCreateView,
    AccountSyncView,
    AccountWebhookRegisterView,
    FixedShippingRateDetailView,
    FixedShippingRateListCreateView,
    LabelProviderAccountDetailView,
    LabelProviderAccountListCreateView,
    MyServiceDefaultsView,
    MyServiceOptionsView,
    MyStoresSyncView,
    RatesView,
    ServiceOptionDetailView,
    ServiceOptionListCreateView,
    ShipStationWebhookView,
    StoreAssignView,
    StoreListView,
    StoreSyncShipFromView,
    StoreSyncView,
    StoreUpdateView,
)

urlpatterns = [
    # ShipStation account management (print center)
    path('accounts/', AccountListCreateView.as_view(), name='ss-account-list'),
    path('accounts/<int:pk>/', AccountDetailView.as_view(), name='ss-account-detail'),
    path('accounts/<int:pk>/sync/', AccountSyncView.as_view(), name='ss-account-sync'),
    path('accounts/<int:pk>/register-webhooks/', AccountWebhookRegisterView.as_view(), name='ss-webhook-register'),

    # Store management (print center)
    path('stores/', StoreListView.as_view(), name='store-list'),
    path('stores/<int:pk>/', StoreUpdateView.as_view(), name='store-update'),
    path('stores/<int:pk>/assign/', StoreAssignView.as_view(), name='store-assign'),
    path('stores/<int:pk>/sync/', StoreSyncView.as_view(), name='store-sync'),
    path('stores/<int:pk>/sync-ship-from/', StoreSyncShipFromView.as_view(), name='store-sync-ship-from'),

    # Sub-user sync trigger
    path('stores/sync-mine/', MyStoresSyncView.as_view(), name='my-stores-sync'),

    # Label provider accounts (print center enables/disables label-purchase sources)
    path('label-providers/', LabelProviderAccountListCreateView.as_view(), name='label-provider-list'),
    path('label-providers/<int:pk>/', LabelProviderAccountDetailView.as_view(), name='label-provider-detail'),

    # Shipping service options (print center configures per category)
    path('service-options/mine/', MyServiceOptionsView.as_view(), name='service-options-mine'),
    path('service-options/', ServiceOptionListCreateView.as_view(), name='service-option-list'),
    path('service-options/<int:pk>/', ServiceOptionDetailView.as_view(), name='service-option-detail'),

    # Shipping service defaults (sub-user picks default per category)
    path('service-defaults/', MyServiceDefaultsView.as_view(), name='service-defaults'),

    # Fixed shipping rates (flat price table, used when pricing_mode == 'fixed')
    path('fixed-rates/', FixedShippingRateListCreateView.as_view(), name='fixed-rate-list'),
    path('fixed-rates/<int:pk>/', FixedShippingRateDetailView.as_view(), name='fixed-rate-detail'),

    # Rates (sub-user)
    path('rates/', RatesView.as_view(), name='shipping-rates'),

    # Webhook receiver (ShipStation calls this when orders arrive)
    path('webhook/<int:account_id>/', ShipStationWebhookView.as_view(), name='ss-webhook'),
]
