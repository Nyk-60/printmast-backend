from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import AddFundsView, BalanceView, TransactionViewSet

router = DefaultRouter()
router.register('transactions', TransactionViewSet, basename='transaction')

urlpatterns = [
    path('balance/', BalanceView.as_view(), name='balance'),
    path('add-funds/', AddFundsView.as_view(), name='add-funds'),
] + router.urls
