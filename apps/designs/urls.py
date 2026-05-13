from rest_framework.routers import DefaultRouter

from .views import DesignViewSet, UserSkuDesignViewSet

router = DefaultRouter()
router.register('sku-mappings', UserSkuDesignViewSet, basename='sku-design')
router.register('', DesignViewSet, basename='design')

urlpatterns = router.urls
