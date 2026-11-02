"""
Role-based permissions.

Two roles, derived from Django's built-in `is_staff` flag:

* **admin**    (is_staff=True)  – manages venues, seats and events (full CRUD, publish,
  cancel), sees every booking, manages users.
* **standard** (is_staff=False) – browses venues and on-sale events, books seats, and
  manages *their own* bookings (view, update contact details, confirm, cancel).
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission


def is_admin(user) -> bool:
    return bool(user and user.is_authenticated and user.is_staff)


class IsAdminOrReadOnly(BasePermission):
    """Any authenticated user may read; only admins may write."""

    message = "Only admin users can modify this resource."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.method in SAFE_METHODS or is_admin(request.user)


class IsOwnerOrAdmin(BasePermission):
    """Object-level: standard users may only touch bookings they made."""

    message = "You can only access your own bookings."

    def has_object_permission(self, request, view, obj):
        return is_admin(request.user) or obj.user_id == request.user.id


class IsAdmin(BasePermission):
    message = "This action requires an admin user."

    def has_permission(self, request, view):
        return is_admin(request.user)
