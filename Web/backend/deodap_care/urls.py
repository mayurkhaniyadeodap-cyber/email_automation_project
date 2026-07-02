"""URL configuration for the DeoDap Care Panel Engine."""

from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from apps.tickets.tracking import conversation_json, tracking_file, tracking_page


def root(_request):
    """Lightweight index/health check so the bare root path doesn't 404."""
    return JsonResponse(
        {
            "service": "DeoDap Care Panel Engine",
            "status": "ok",
            "endpoints": ["/admin/", "/api/", "/api/v1/", "/api/auth/", "/t"],
        }
    )


urlpatterns = [
    path("", root, name="root"),
    path("admin/", admin.site.urls),
    path("api/", include("deodap_care.api")),
    # Versioned alias: clients calling /api/v1/... hit the same endpoints as /api/...
    path("api/v1/", include("deodap_care.api")),
    # DRF browsable-API login (mounted once so the api include can be reused above).
    path("api/auth/", include("rest_framework.urls")),
    # Public customer tracking portal (internal-fallback tracking links point here).
    path("t", tracking_page, name="tracking-page"),
    path("t/file", tracking_file, name="tracking-file"),   # scoped media serving
    # JSON conversation feed for the external Care Panel admin's Conversation tab (same data
    # as the customer portal; reuses _build_conversation).
    path("t/conversation", conversation_json, name="tracking-conversation"),
]
