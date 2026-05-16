from django.urls import path

from .views import (
    FinanceOverviewView,
    MyTransactionExportView,
    MyTransactionListView,
    MySummaryView,
    SubUserSummaryView,
    SubUserTransactionExportView,
    SubUserTransactionListView,
)

urlpatterns = [
    # Sub-user: own finance
    path('my/summary/', MySummaryView.as_view(), name='my-finance-summary'),
    path('my/transactions/', MyTransactionListView.as_view(), name='my-transactions'),
    path('my/download/', MyTransactionExportView.as_view(), name='my-transactions-export'),

    # Print center: all sub-users overview
    path('overview/', FinanceOverviewView.as_view(), name='finance-overview'),

    # Print center: per sub-user
    path('sub-users/<int:pk>/summary/', SubUserSummaryView.as_view(), name='sub-finance-summary'),
    path('sub-users/<int:pk>/transactions/', SubUserTransactionListView.as_view(), name='sub-transactions'),
    path('sub-users/<int:pk>/download/', SubUserTransactionExportView.as_view(), name='sub-transactions-export'),
]
