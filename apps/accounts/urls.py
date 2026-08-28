from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    AddBalanceView,
    PCShippingSettingsView,
    InvoiceDetailView,
    InvoiceListView,
    LoginView,
    LogoutView,
    MeView,
    MyShippingRuleDeleteView,
    MyShippingRulesView,
    MyTransactionsView,
    PrintCenterAnalyticsView,
    PrintCenterCreateView,
    PrintCenterDetailView,
    PrintCenterImpersonateView,
    PrintCenterListView,
    PrintCenterPricingTierDetailView,
    PrintCenterPricingTierView,
    RegisterView,
    SubUserDetailView,
    SubUserListCreateView,
    SubUserToggleActiveView,
    SubUserTransactionsView,
    SuperAdminDashboardStatsView,
)

urlpatterns = [
    # Auth
    path('register/', RegisterView.as_view(), name='auth-register'),
    path('login/', LoginView.as_view(), name='auth-login'),
    path('logout/', LogoutView.as_view(), name='auth-logout'),
    path('me/', MeView.as_view(), name='auth-me'),
    path('refresh/', TokenRefreshView.as_view(), name='auth-refresh'),

    # SuperAdmin — dashboard stats
    path('superadmin/dashboard-stats/', SuperAdminDashboardStatsView.as_view(), name='superadmin-stats'),

    # SuperAdmin — print center management
    path('superadmin/print-centers/', PrintCenterListView.as_view(), name='superadmin-pc-list'),
    path('superadmin/print-centers/create/', PrintCenterCreateView.as_view(), name='superadmin-pc-create'),
    path('superadmin/print-centers/<int:pk>/', PrintCenterDetailView.as_view(), name='superadmin-pc-detail'),
    path('superadmin/print-centers/<int:pk>/impersonate/', PrintCenterImpersonateView.as_view(), name='superadmin-pc-impersonate'),
    path('superadmin/print-centers/<int:pk>/analytics/', PrintCenterAnalyticsView.as_view(), name='superadmin-pc-analytics'),
    path('superadmin/print-centers/<int:pk>/tiers/', PrintCenterPricingTierView.as_view(), name='superadmin-pc-tiers'),
    path('superadmin/print-centers/<int:pk>/tiers/<int:tier_pk>/', PrintCenterPricingTierDetailView.as_view(), name='superadmin-pc-tier-detail'),

    # SuperAdmin — invoices
    path('superadmin/invoices/', InvoiceListView.as_view(), name='superadmin-invoices'),
    path('superadmin/invoices/<int:invoice_pk>/', InvoiceDetailView.as_view(), name='superadmin-invoice-detail'),

    # Print Center — shipping settings
    path('print-center/shipping-settings/', PCShippingSettingsView.as_view(), name='pc-shipping-settings'),

    # Print Center — sub user management
    path('print-center/sub-users/', SubUserListCreateView.as_view(), name='pc-sub-user-list'),
    path('print-center/sub-users/<int:pk>/', SubUserDetailView.as_view(), name='pc-sub-user-detail'),
    path('print-center/sub-users/<int:pk>/toggle-active/', SubUserToggleActiveView.as_view(), name='pc-sub-user-toggle'),
    path('print-center/sub-users/<int:pk>/add-balance/', AddBalanceView.as_view(), name='pc-sub-user-add-balance'),
    path('print-center/sub-users/<int:pk>/transactions/', SubUserTransactionsView.as_view(), name='pc-sub-user-transactions'),

    # Sub User — self-service
    path('my/transactions/', MyTransactionsView.as_view(), name='my-transactions'),
    path('my/shipping-rules/', MyShippingRulesView.as_view(), name='my-shipping-rules'),
    path('my/shipping-rules/<int:rule_id>/', MyShippingRuleDeleteView.as_view(), name='my-shipping-rule-delete'),
]
