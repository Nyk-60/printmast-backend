from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AutoMatchAllView,
    MappingCountView,
    MappingExportView,
    MappingGenerateTemplateView,
    MappingTemplateView,
    MappingUploadView,
    ProductionQueueView,
    ProductMappingDeleteView,
    ProductMappingListView,
    OrderViewSet,
)

router = DefaultRouter()
router.register('', OrderViewSet, basename='order')

urlpatterns = [
    path('auto-match-all/', AutoMatchAllView.as_view(), name='auto-match-all'),
    path('mappings/', ProductMappingListView.as_view(), name='mapping-list'),
    path('mappings/count/', MappingCountView.as_view(), name='mapping-count'),
    path('mappings/<int:pk>/', ProductMappingDeleteView.as_view(), name='mapping-delete'),
    path('mappings/template/', MappingTemplateView.as_view(), name='mapping-template'),
    path('mappings/generate-template/', MappingGenerateTemplateView.as_view(), name='mapping-generate-template'),
    path('mappings/export/', MappingExportView.as_view(), name='mapping-export'),
    path('mappings/upload/', MappingUploadView.as_view(), name='mapping-upload'),
    path('production-queue/', ProductionQueueView.as_view(), name='production-queue'),
] + router.urls
