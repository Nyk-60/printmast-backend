from django.urls import path

from .views import (
    SkuDesignDetailView,
    SkuDesignListCreateView,
)

urlpatterns = [
    path('sku/', SkuDesignListCreateView.as_view(), name='sku-design-list'),
    path('sku/<int:pk>/', SkuDesignDetailView.as_view(), name='sku-design-detail'),
]
