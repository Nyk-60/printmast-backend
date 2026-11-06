from django.urls import path

from .views import (
    ReshipRequestApproveView,
    ReshipRequestCreateView,
    ReshipRequestListView,
    ReshipRequestRejectView,
    ReshipRequestRetryPushView,
)

urlpatterns = [
    path('', ReshipRequestListView.as_view(), name='reship-list'),
    path('create/', ReshipRequestCreateView.as_view(), name='reship-create'),
    path('<int:pk>/approve/', ReshipRequestApproveView.as_view(), name='reship-approve'),
    path('<int:pk>/reject/', ReshipRequestRejectView.as_view(), name='reship-reject'),
    path('<int:pk>/retry-push/', ReshipRequestRetryPushView.as_view(), name='reship-retry-push'),
]
