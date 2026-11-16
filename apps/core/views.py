from rest_framework.generics import RetrieveAPIView

from .models import PlatformBanner
from .serializers import PlatformBannerSerializer


class PlatformBannerView(RetrieveAPIView):
    serializer_class = PlatformBannerSerializer

    def get_object(self):
        return PlatformBanner.load()
