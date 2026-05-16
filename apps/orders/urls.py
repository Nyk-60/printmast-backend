from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    MappingUploadView,
    ProductionQueueView,
    ProductMappingDeleteView,
    ProductMappingListView,
    OrderViewSet,
)

router = DefaultRouter()
router.register('', OrderViewSet, basename='order')

urlpatterns = router.urls + [
    path('mappings/', ProductMappingListView.as_view(), name='mapping-list'),
    path('mappings/<int:pk>/', ProductMappingDeleteView.as_view(), name='mapping-delete'),
    path('mappings/upload/', MappingUploadView.as_view(), name='mapping-upload'),
    path('production-queue/', ProductionQueueView.as_view(), name='production-queue'),
]
