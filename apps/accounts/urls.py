from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    AddBalanceView,
    LoginView,
    LogoutView,
    MeView,
    MyShippingRuleDeleteView,
    MyShippingRulesView,
    MyTransactionsView,
    PrintCenterDetailView,
    PrintCenterListView,
    RegisterView,
    SubUserDetailView,
    SubUserListCreateView,
    SubUserToggleActiveView,
    SubUserTransactionsView,
)

urlpatterns = [
    # Auth
    path('register/', RegisterView.as_view(), name='auth-register'),
    path('login/', LoginView.as_view(), name='auth-login'),
    path('logout/', LogoutView.as_view(), name='auth-logout'),
    path('me/', MeView.as_view(), name='auth-me'),
    path('refresh/', TokenRefreshView.as_view(), name='auth-refresh'),

    # SuperAdmin — print center management
    path('superadmin/print-centers/', PrintCenterListView.as_view(), name='superadmin-pc-list'),
    path('superadmin/print-centers/<int:pk>/', PrintCenterDetailView.as_view(), name='superadmin-pc-detail'),

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
