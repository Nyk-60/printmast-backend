from django.urls import path

from .views import PlatformBannerView

urlpatterns = [
    path('banner/', PlatformBannerView.as_view(), name='platform-banner'),
]
