from rest_framework import serializers

from .models import PlatformBanner


class PlatformBannerSerializer(serializers.ModelSerializer):
    video_url = serializers.SerializerMethodField()

    class Meta:
        model = PlatformBanner
        fields = ('video_url',)

    def get_video_url(self, obj):
        if not obj.video:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.video.url) if request else obj.video.url
