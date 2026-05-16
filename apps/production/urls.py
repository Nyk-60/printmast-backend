from django.urls import path

from .views import BatchDetailView, BatchListCreateView

urlpatterns = [
    path('batches/', BatchListCreateView.as_view(), name='batch-list'),
    path('batches/<int:pk>/', BatchDetailView.as_view(), name='batch-detail'),
]
