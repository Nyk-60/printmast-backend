from django.urls import path

from .views import (
    BatchActionView,
    BatchDetailView,
    BatchListCreateView,
    BatchOrderActionView,
    PreflightView,
)

urlpatterns = [
    path('batches/', BatchListCreateView.as_view(), name='batch-list'),
    path('batches/preflight/', PreflightView.as_view(), name='batch-preflight'),
    path('batches/<int:pk>/', BatchDetailView.as_view(), name='batch-detail'),
    path('batches/<int:pk>/orders/<int:order_id>/<str:action>/', BatchOrderActionView.as_view(), name='batch-order-action'),
    path('batches/<int:pk>/<str:action>/', BatchActionView.as_view(), name='batch-action'),
]
