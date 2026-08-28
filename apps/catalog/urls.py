from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BrandViewSet,
    CategoryViewSet,
    MyCatalogProductView,
    MyCatalogView,
    PrintAreaDimsView,
    SubCatalogColorClassificationView,
    SubCatalogExportView,
    PlatformVariantDetailView,
    PlatformVariantListCreateView,
    PrintCenterCatalogView,
    PrintCenterPrintAreaCreateView,
    PrintCenterPrintAreaView,
    PrintCenterProductCreateView,
    PrintCenterProductView,
    PrintCenterVariantCreateView,
    PrintCenterVariantView,
    ProductTemplateView,
    ProductUploadView,
    ProductViewSet,
    VariantDimensionRuleDetailView,
    VariantDimensionRuleListCreateView,
)

router = DefaultRouter()
router.register('categories', CategoryViewSet, basename='category')
router.register('brands', BrandViewSet, basename='brand')
router.register('platform-products', ProductViewSet, basename='platform-product')

urlpatterns = [
    # These must come BEFORE router.urls so 'template' and 'upload' are not
    # captured as {pk} by the router's platform-products/<pk>/ pattern.
    path('platform-products/template/', ProductTemplateView.as_view(), name='product-template'),
    path('platform-products/upload/', ProductUploadView.as_view(), name='product-upload'),
    path('platform-products/<int:pk>/variants/', PlatformVariantListCreateView.as_view(), name='platform-variant-list'),
    path('platform-products/<int:pk>/variants/<int:variant_pk>/', PlatformVariantDetailView.as_view(), name='platform-variant-detail'),
] + router.urls + [

    # Print center catalog management
    path('my-catalog/', PrintCenterCatalogView.as_view(), name='pc-catalog-list'),
    path('my-catalog/create/', PrintCenterProductCreateView.as_view(), name='pc-catalog-create'),
    path('my-catalog/<int:pk>/', PrintCenterProductView.as_view(), name='pc-catalog-detail'),
    path('my-catalog/<int:pk>/variants/', PrintCenterVariantCreateView.as_view(), name='pc-variant-create'),
    path('my-catalog/<int:pk>/variants/<int:variant_pk>/', PrintCenterVariantView.as_view(), name='pc-variant-detail'),
    path('my-catalog/<int:pk>/variants/<int:variant_pk>/rules/', VariantDimensionRuleListCreateView.as_view(), name='pc-variant-rules'),
    path('my-catalog/<int:pk>/variants/<int:variant_pk>/rules/<int:rule_pk>/', VariantDimensionRuleDetailView.as_view(), name='pc-variant-rule-detail'),
    path('my-catalog/<int:pk>/print-areas/', PrintCenterPrintAreaCreateView.as_view(), name='pc-area-create'),
    path('my-catalog/<int:pk>/print-areas/<int:area_pk>/', PrintCenterPrintAreaView.as_view(), name='pc-area-detail'),
    path('my-catalog/<int:pk>/print-area-dims/', PrintAreaDimsView.as_view(), name='pc-area-dims'),

    # Sub-user: read-only view of their print center's catalog
    path('sub-catalog/', MyCatalogView.as_view(), name='sub-catalog-list'),
    path('sub-catalog/export/', SubCatalogExportView.as_view(), name='sub-catalog-export'),
    path('sub-catalog/<int:pk>/', MyCatalogProductView.as_view(), name='sub-catalog-detail'),
    path('sub-catalog/<int:pk>/color-classification/', SubCatalogColorClassificationView.as_view(), name='sub-catalog-color-classification'),
]
