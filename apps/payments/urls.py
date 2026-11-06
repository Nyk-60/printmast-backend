from django.urls import path

from .views import (
    FinanceOverviewView,
    MyInvoiceListView,
    MyTransactionExportView,
    MyTransactionFilterOptionsView,
    MyTransactionListView,
    MySummaryView,
    PrintCenterFinanceSummaryView,
    PrintCenterSubUserInvoiceListView,
    PrintCenterTransactionExportView,
    PrintCenterTransactionFilterOptionsView,
    PrintCenterTransactionListView,
    SubUserInvoiceDetailView,
    SubUserInvoiceListView,
    SubUserSummaryView,
    SubUserTransactionExportView,
    SubUserTransactionListView,
)

urlpatterns = [
    # Sub-user: own finance
    path('my/summary/', MySummaryView.as_view(), name='my-finance-summary'),
    path('my/transactions/', MyTransactionListView.as_view(), name='my-transactions'),
    path('my/download/', MyTransactionExportView.as_view(), name='my-transactions-export'),
    path('my/transactions/filter-options/', MyTransactionFilterOptionsView.as_view(), name='my-transactions-filter-options'),
    path('my/invoices/', MyInvoiceListView.as_view(), name='my-invoices'),

    # Print center: all sub-users overview
    path('overview/', FinanceOverviewView.as_view(), name='finance-overview'),

    # Print center: own combined finance (every sub-user, or ?sub_user=<id>)
    path('pc/summary/', PrintCenterFinanceSummaryView.as_view(), name='pc-finance-summary'),
    path('pc/transactions/', PrintCenterTransactionListView.as_view(), name='pc-transactions'),
    path('pc/transactions/filter-options/', PrintCenterTransactionFilterOptionsView.as_view(), name='pc-transactions-filter-options'),
    path('pc/download/', PrintCenterTransactionExportView.as_view(), name='pc-transactions-export'),
    path('pc/sub-user-invoices/', PrintCenterSubUserInvoiceListView.as_view(), name='pc-sub-user-invoices'),

    # Print center: per sub-user
    path('sub-users/<int:pk>/summary/', SubUserSummaryView.as_view(), name='sub-finance-summary'),
    path('sub-users/<int:pk>/transactions/', SubUserTransactionListView.as_view(), name='sub-transactions'),
    path('sub-users/<int:pk>/download/', SubUserTransactionExportView.as_view(), name='sub-transactions-export'),
    path('sub-users/<int:pk>/invoices/', SubUserInvoiceListView.as_view(), name='sub-invoices'),
    path('sub-users/<int:pk>/invoices/<int:invoice_pk>/', SubUserInvoiceDetailView.as_view(), name='sub-invoice-detail'),
]
