from django.contrib import admin

from .models import PlatformBanner


@admin.register(PlatformBanner)
class PlatformBannerAdmin(admin.ModelAdmin):
    list_display = ('video', 'updated_at')

    def has_add_permission(self, request):
        return not PlatformBanner.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
