from rest_framework.permissions import BasePermission

from .models import PrintCenterProfile
from .scoping import get_sub_user_scope


class IsSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_superadmin
        )


class IsPrintCenter(BasePermission):
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated and request.user.is_print_center):
            return False
        try:
            return request.user.print_center_profile.status == PrintCenterProfile.Status.ACTIVE
        except PrintCenterProfile.DoesNotExist:
            return False


class IsSubUser(BasePermission):
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated and request.user.is_sub_user):
            return False
        try:
            return request.user.sub_user_profile.is_active
        except Exception:
            return False


class IsSubUserOrTeamMember(BasePermission):
    """Like IsSubUser, but also admits an active Team Member — for every
    view a Team Member should share with the sub-user whose data they're
    scoped to (see apps.accounts.scoping.get_sub_user_scope)."""
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        sub_user, _ = get_sub_user_scope(request.user)
        return sub_user is not None


class IsPrintCenterOrSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_superadmin:
            return True
        if request.user.is_print_center:
            try:
                return request.user.print_center_profile.status == PrintCenterProfile.Status.ACTIVE
            except PrintCenterProfile.DoesNotExist:
                return False
        return False
