"""
Team-member management API (Admin only). Maps the auth User + UserProfile to a
single "team member" resource with role + lockout + audit logging.

    GET    /api/users/                 list members
    POST   /api/users/                 add member (USER_CREATED)
    PATCH  /api/users/<id>/            edit name/role/is_active (USER_DISABLED/ENABLED)
    POST   /api/users/<id>/reset_password/   (PASSWORD_RESET)
    POST   /api/users/<id>/unlock/           clear a lockout
"""

from django.contrib.auth import get_user_model
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from .models import UserAuditLog, UserProfile

User = get_user_model()


class IsAdminRole(BasePermission):
    message = "Admin role required."

    def has_permission(self, request, view):
        u = request.user
        if not u.is_authenticated:
            return False
        if u.is_superuser:
            return True
        prof = getattr(u, "profile", None)
        return bool(prof and prof.role == UserProfile.ROLE_ADMIN)


class TeamMemberSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="profile.name", required=False, allow_blank=True)
    role = serializers.ChoiceField(source="profile.role", choices=UserProfile.ROLE_CHOICES,
                                   required=False)
    is_locked = serializers.BooleanField(source="profile.is_locked", read_only=True)
    password = serializers.CharField(write_only=True, required=False, min_length=6)
    created_at = serializers.DateTimeField(source="date_joined", read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "name", "role", "is_active", "is_locked",
                  "password", "created_at", "last_login"]
        read_only_fields = ["id", "created_at", "last_login", "is_locked"]

    def validate_username(self, value):
        value = value.strip()
        qs = User.objects.filter(username__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Username already taken.")
        return value

    def create(self, validated):
        profile_data = validated.pop("profile", {})
        password = validated.pop("password", None) or "Changeme@123"
        user = User.objects.create(username=validated["username"].strip(),
                                   is_active=validated.get("is_active", True))
        user.set_password(password)
        user.save()
        UserProfile.objects.update_or_create(
            user=user,
            defaults={"name": profile_data.get("name") or user.username,
                      "role": profile_data.get("role") or UserProfile.ROLE_AGENT})
        return user

    def update(self, user, validated):
        profile_data = validated.pop("profile", {})
        validated.pop("password", None)        # password only via reset_password
        validated.pop("username", None)        # username is immutable
        if "is_active" in validated:
            user.is_active = validated["is_active"]
        user.save()
        prof = user.profile
        if "name" in profile_data:
            prof.name = profile_data["name"]
        if "role" in profile_data:
            prof.role = profile_data["role"]
        prof.save()
        return user


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.select_related("profile").order_by("id")
    serializer_class = TeamMemberSerializer
    permission_classes = [IsAdminRole]

    def _audit(self, event, target, **detail):
        UserAuditLog.objects.create(actor=self.request.user.get_username(),
                                    event=event, target=target, detail=detail)

    def perform_create(self, serializer):
        user = serializer.save()
        self._audit(UserAuditLog.USER_CREATED, user.username, role=user.profile.role)

    def perform_update(self, serializer):
        was_active = serializer.instance.is_active
        user = serializer.save()
        if was_active and not user.is_active:
            self._audit(UserAuditLog.USER_DISABLED, user.username)
        elif not was_active and user.is_active:
            self._audit(UserAuditLog.USER_ENABLED, user.username)
        else:
            self._audit(UserAuditLog.USER_UPDATED, user.username, role=user.profile.role)

    def destroy(self, request, *args, **kwargs):
        """Permanently delete a member (with guards). Use PATCH is_active=False to
        merely disable instead."""
        user = self.get_object()
        if user == request.user:
            return Response({"detail": "You cannot delete your own account."},
                            status=status.HTTP_400_BAD_REQUEST)
        is_admin = user.is_superuser or getattr(user.profile, "role", "") == UserProfile.ROLE_ADMIN
        if is_admin:
            from django.db.models import Q

            others = (User.objects.filter(Q(is_superuser=True)
                      | Q(profile__role=UserProfile.ROLE_ADMIN))
                      .exclude(pk=user.pk).filter(is_active=True).count())
            if others == 0:
                return Response({"detail": "Cannot delete the last admin."},
                                status=status.HTTP_400_BAD_REQUEST)
        username = user.username
        user.delete()
        self._audit(UserAuditLog.USER_DELETED, username)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def reset_password(self, request, pk=None):
        user = self.get_object()
        new = request.data.get("password") or ""
        if len(new) < 6:
            return Response({"detail": "Password must be at least 6 characters."},
                            status=status.HTTP_400_BAD_REQUEST)
        user.set_password(new)
        user.save()
        # Clear any lockout when an admin resets the password.
        prof = user.profile
        prof.failed_attempts, prof.is_locked = 0, False
        prof.save(update_fields=["failed_attempts", "is_locked", "updated_at"])
        self._audit(UserAuditLog.PASSWORD_RESET, user.username)
        return Response({"detail": "Password reset."})

    @action(detail=True, methods=["post"])
    def unlock(self, request, pk=None):
        user = self.get_object()
        prof = user.profile
        prof.failed_attempts, prof.is_locked = 0, False
        prof.save(update_fields=["failed_attempts", "is_locked", "updated_at"])
        return Response({"detail": "Unlocked."})
