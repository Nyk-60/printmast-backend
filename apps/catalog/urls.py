from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BrandViewSet,
    CategoryViewSet,
    MyCatalogProductView,
    MyCatalogView,
    PrintCenterCatalogView,
    PrintCenterPrintAreaCreateView,
    PrintCenterPrintAreaView,
    PrintCenterProductCreateView,
    PrintCenterProductView,
    PrintCenterVariantCreateView,
    PrintCenterVariantView,
    ProductViewSet,
)

router = DefaultRouter()
router.register('categories', CategoryViewSet, basename='category')
router.register('brands', BrandViewSet, basename='brand')
router.register('platform-products', ProductViewSet, basename='platform-product')

urlpatterns = router.urls + [
    # Print center catalog management
    path('my-catalog/', PrintCenterCatalogView.as_view(), name='pc-catalog-list'),
    path('my-catalog/create/', PrintCenterProductCreateView.as_view(), name='pc-catalog-create'),
    path('my-catalog/<int:pk>/', PrintCenterProductView.as_view(), name='pc-catalog-detail'),
    path('my-catalog/<int:pk>/variants/', PrintCenterVariantCreateView.as_view(), name='pc-variant-create'),
    path('my-catalog/<int:pk>/variants/<int:variant_pk>/', PrintCenterVariantView.as_view(), name='pc-variant-detail'),
    path('my-catalog/<int:pk>/print-areas/', PrintCenterPrintAreaCreateView.as_view(), name='pc-area-create'),
    path('my-catalog/<int:pk>/print-areas/<int:area_pk>/', PrintCenterPrintAreaView.as_view(), name='pc-area-detail'),

    # Sub-user: read-only view of their print center's catalog
    path('sub-catalog/', MyCatalogView.as_view(), name='sub-catalog-list'),
    path('sub-catalog/<int:pk>/', MyCatalogProductView.as_view(), name='sub-catalog-detail'),
]
