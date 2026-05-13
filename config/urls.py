from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

api_v1 = [
    path('auth/', include('apps.accounts.urls')),
    path('catalog/', include('apps.catalog.urls')),
    path('orders/', include('apps.orders.urls')),
    path('designs/', include('apps.designs.urls')),
    path('payments/', include('apps.payments.urls')),
    path('shipping/', include('apps.shipping.urls')),
]

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include(api_v1)),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='docs'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
