"""Central DRF router + auth endpoints for the care panel API."""

from django.conf import settings as dj_settings
from django.contrib.auth import get_user_model
from django.urls import include, path
from rest_framework import routers
from rest_framework.authtoken.models import Token
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.brand_settings.views import (
    BlockListEntryViewSet,
    BrandSettingsViewSet,
    SupportEmailViewSet,
)
from apps.analytics import views as analytics_views
from apps.ingestion.views import gmail_webhook
from apps.integrations.webhooks import care_panel_webhook
from apps.ingestion.oauth_views import gmail_callback, gmail_connect, gmail_fetch
from apps.organizations.views import (
    BrandViewSet,
    MailboxViewSet,
    OrganizationViewSet,
)
from apps.organizations.user_views import UserViewSet
from apps.taxonomy.views import (
    CategoryViewSet,
    RuleViewSet,
    SubTopicViewSet,
    TemplateViewSet,
)
from apps.tickets.views import (
    AuditLogEntryViewSet,
    ComposedEmailViewSet,
    EscalationViewSet,
    InternalEmailViewSet,
    MessageViewSet,
    TicketViewSet,
    attachment_file,
)
from apps.tickets.pending_views import PendingConversationViewSet

router = routers.DefaultRouter()
router.register("organizations", OrganizationViewSet, basename="organization")
router.register("brands", BrandViewSet, basename="brand")
router.register("mailboxes", MailboxViewSet, basename="mailbox")
router.register("categories", CategoryViewSet, basename="category")
router.register("sub-topics", SubTopicViewSet, basename="subtopic")
router.register("rules", RuleViewSet, basename="rule")
router.register("templates", TemplateViewSet, basename="template")
router.register("settings", BrandSettingsViewSet, basename="brandsettings")
router.register("block-list", BlockListEntryViewSet, basename="blocklistentry")
router.register("support-emails", SupportEmailViewSet, basename="supportemail")
router.register("tickets", TicketViewSet, basename="ticket")
router.register("messages", MessageViewSet, basename="message")
router.register("audit-log", AuditLogEntryViewSet, basename="auditlogentry")
router.register("users", UserViewSet, basename="user")
router.register("pending", PendingConversationViewSet, basename="pending")
router.register("escalations", EscalationViewSet, basename="escalation")
router.register("internal-emails", InternalEmailViewSet, basename="internalemail")
router.register("compose-emails", ComposedEmailViewSet, basename="composedemail")


def _get_profile(user):
    """Return (creating if needed) the user's profile. Superusers default to admin."""
    from apps.organizations.models import UserProfile

    profile, created = UserProfile.objects.get_or_create(user=user)
    if created:
        profile.name = user.get_full_name() or user.get_username()
        profile.role = UserProfile.ROLE_ADMIN if user.is_superuser else UserProfile.ROLE_AGENT
        profile.save()
    return profile


def _user_payload(user):
    """Shared user info for login + /auth/me (name, role, permissions, orgs)."""
    from apps.organizations.models import Organization

    profile = _get_profile(user)
    orgs = (Organization.objects.all() if user.is_superuser
            else user.organizations.all())
    return {
        "id": user.id,
        "username": user.get_username(),
        "name": profile.name or user.get_username(),
        "email": user.email,
        "role": profile.effective_role,
        "role_display": dict(profile.ROLE_CHOICES).get(profile.effective_role, "Agent"),
        "permissions": {"nav": profile.nav, "read_only": profile.read_only},
        "is_superuser": user.is_superuser,
        "auto_login": bool(getattr(dj_settings, "AUTO_LOGIN", False)),
        "email_provider": getattr(dj_settings, "EMAIL_PROVIDER", "imap"),
        "imap_configured": bool(
            getattr(dj_settings, "IMAP_HOST", "") and getattr(dj_settings, "IMAP_USER", "")
        ),
        "organizations": [{"id": o.id, "name": o.name, "slug": o.slug} for o in orgs],
    }


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def login_view(request):
    """Username/password login with field validation + account lockout after 5
    failed attempts. Returns an auth token + the user's role and permissions."""
    username = (request.data.get("username") or "").strip()
    password = request.data.get("password") or ""
    if not username or not password:
        return Response({"detail": "Username and password are required."}, status=400)

    User = get_user_model()
    user = User.objects.filter(username__iexact=username).first()
    if user is None:
        return Response({"detail": "Invalid username or password."}, status=401)

    profile = _get_profile(user)
    if not user.is_active or profile.is_locked:
        return Response(
            {"detail": "Account is locked. Please contact an administrator."},
            status=403,
        )

    if not user.check_password(password):
        profile.failed_attempts += 1
        if profile.failed_attempts >= profile.MAX_FAILED_ATTEMPTS:
            profile.is_locked = True
        profile.save(update_fields=["failed_attempts", "is_locked", "updated_at"])
        if profile.is_locked:
            return Response(
                {"detail": "Account locked after 5 failed attempts. Contact an administrator."},
                status=403,
            )
        left = profile.MAX_FAILED_ATTEMPTS - profile.failed_attempts
        return Response(
            {"detail": f"Invalid username or password. {left} attempt(s) left."},
            status=401,
        )

    # Success -> reset counter, stamp last_login, issue token, audit.
    from django.utils import timezone

    from apps.organizations.models import UserAuditLog

    if profile.failed_attempts:
        profile.failed_attempts = 0
        profile.save(update_fields=["failed_attempts", "updated_at"])
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    token, _ = Token.objects.get_or_create(user=user)
    UserAuditLog.objects.create(actor=user.get_username(), event=UserAuditLog.USER_LOGIN,
                                target=user.get_username())
    from apps.analytics.logging import log_login
    log_login(user, request)
    return Response({"token": token.key, "user": _user_payload(user)})


@api_view(["GET", "POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def guest_login(request):
    """Auto-login (no credentials) when AUTO_LOGIN is on -- opens the panel without a
    sign-in screen as AUTO_LOGIN_USER (default 'admin')."""
    if not getattr(dj_settings, "AUTO_LOGIN", False):
        return Response({"detail": "Auto-login is disabled."}, status=403)
    User = get_user_model()
    username = getattr(dj_settings, "AUTO_LOGIN_USER", "admin")
    user = (User.objects.filter(username=username, is_active=True).first()
            or User.objects.filter(is_superuser=True, is_active=True).first())
    if user is None:
        return Response({"detail": "No auto-login user available."}, status=404)
    token, _ = Token.objects.get_or_create(user=user)
    return Response({"token": token.key, "user": _user_payload(user)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout_view(request):
    """Log out: invalidate the auth token and record USER_LOGOUT."""
    from apps.organizations.models import UserAuditLog

    user = request.user
    UserAuditLog.objects.create(actor=user.get_username(), event=UserAuditLog.USER_LOGOUT,
                                target=user.get_username())
    from apps.analytics.logging import log_logout
    log_logout(user)
    Token.objects.filter(user=user).delete()
    return Response({"detail": "Logged out."})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    """Current user + role/permissions + the organizations they can scope to."""
    return Response(_user_payload(request.user))


urlpatterns = [
    path("", include(router.urls)),
    path("auth/token/", login_view, name="api-token"),
    path("auth/login/", login_view, name="api-login"),
    path("auth/guest/", guest_login, name="api-guest"),
    path("auth/logout/", logout_view, name="api-logout"),
    path("auth/me/", me, name="api-me"),
    path("gmail/webhook", gmail_webhook, name="gmail-webhook"),
    path("care-panel/webhook", care_panel_webhook, name="care-panel-webhook"),
    path("gmail/connect/", gmail_connect, name="gmail-connect"),
    path("gmail/callback/", gmail_callback, name="gmail-callback"),
    path("gmail/fetch/", gmail_fetch, name="gmail-fetch"),
    path("attachments/<int:pk>/", attachment_file, name="attachment-file"),
    path("analytics/overview/", analytics_views.overview, name="analytics-overview"),
    path("analytics/volume/", analytics_views.volume, name="analytics-volume"),
    path("analytics/sla/", analytics_views.sla, name="analytics-sla"),
    path("analytics/ai-accuracy/", analytics_views.ai_accuracy, name="analytics-ai"),
    path("analytics/agents/", analytics_views.agents, name="analytics-agents"),
    path("analytics/dashboard/", analytics_views.manager_dashboard, name="analytics-dashboard"),
    path("analytics/employee-performance/", analytics_views.employee_performance,
         name="analytics-employee-performance"),
    path("analytics/manual-replies/", analytics_views.manual_reply_report,
         name="analytics-manual-replies"),
    path("analytics/auto-replies/", analytics_views.auto_reply_report,
         name="analytics-auto-replies"),
    path("analytics/login-history/", analytics_views.login_history,
         name="analytics-login-history"),
    path("analytics/internal-metrics/", analytics_views.internal_metrics,
         name="analytics-internal-metrics"),
]
