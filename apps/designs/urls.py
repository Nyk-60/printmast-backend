from django.urls import path

from .views import (
    ColorCategoryListView,
    ColorCategoryUploadView,
    SkuDesignDetailView,
    SkuDesignListCreateView,
)

urlpatterns = [
    path('sku/', SkuDesignListCreateView.as_view(), name='sku-design-list'),
    path('sku/<int:pk>/', SkuDesignDetailView.as_view(), name='sku-design-detail'),
    path('color-categories/', ColorCategoryListView.as_view(), name='color-category-list'),
    path('color-categories/upload/', ColorCategoryUploadView.as_view(), name='color-category-upload'),
]
