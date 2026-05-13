from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from .models import Design, UserSkuDesign
from .serializers import DesignSerializer, UserSkuDesignSerializer


class DesignViewSet(viewsets.ModelViewSet):
    serializer_class = DesignSerializer
    permission_classes = (IsAuthenticated,)

    def get_queryset(self):
        return Design.objects.filter(owner=self.request.user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class UserSkuDesignViewSet(viewsets.ModelViewSet):
    serializer_class = UserSkuDesignSerializer
    permission_classes = (IsAuthenticated,)

    def get_queryset(self):
        return UserSkuDesign.objects.filter(owner=self.request.user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)
