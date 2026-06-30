"""
Browser-based "Connect Gmail" flow (simpler than Pub/Sub for local/dev use):

    GET  /api/gmail/connect/?mailbox=<id>   -> redirect to Google consent
    GET  /api/gmail/callback/               -> Google redirects back; store tokens
    POST /api/gmail/fetch/?mailbox=<id>     -> pull recent mail into tickets now

The agent clicks "Connect Gmail" in the panel, authorizes the mailbox in the
browser, and then "Fetch" pulls mail on demand (no Cloud Pub/Sub, no public URL).

Requires GOOGLE_OAUTH_CLIENT_ID / _SECRET in .env and, in Google Cloud, an OAuth
client (Web type) with this redirect URI registered:
    http://127.0.0.1:8000/api/gmail/callback/
"""

import logging
import os

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.organizations.models import Mailbox

from . import service
from .gmail_client import GMAIL_SCOPE

logger = logging.getLogger(__name__)

# Allow http://localhost callbacks and relax exact-scope matching for local dev.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def _client_config(redirect_uri):
    return {
        "web": {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def _callback_url(request):
    return request.build_absolute_uri(reverse("gmail-callback"))


def _page(title, body):
    return HttpResponse(
        f"<html><body style='font-family:sans-serif;max-width:520px;margin:60px auto'>"
        f"<h2>{title}</h2><p>{body}</p>"
        f"<p>You can close this tab and return to the Care Panel.</p></body></html>"
    )


def gmail_connect(request):
    """Start the OAuth consent flow for a mailbox (browser redirect)."""
    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        return _page("Not configured",
                     "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in backend/.env.")

    mailbox_id = request.GET.get("mailbox")
    mailbox = Mailbox.objects.filter(pk=mailbox_id).first() if mailbox_id else \
        Mailbox.objects.first()
    if mailbox is None:
        return _page("No mailbox", "No mailbox found to connect.")

    from google_auth_oauthlib.flow import Flow

    redirect_uri = _callback_url(request)
    flow = Flow.from_client_config(
        _client_config(redirect_uri), scopes=[GMAIL_SCOPE], redirect_uri=redirect_uri
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=str(mailbox.id),
        login_hint=mailbox.email_address,
    )
    return redirect(auth_url)


def gmail_callback(request):
    """Google redirects here with ?code & ?state(=mailbox id); store the tokens."""
    error = request.GET.get("error")
    if error:
        return _page("Authorization failed", f"Google returned: {error}")

    code = request.GET.get("code")
    mailbox_id = request.GET.get("state")
    mailbox = Mailbox.objects.filter(pk=mailbox_id).first()
    if not code or mailbox is None:
        return _page("Invalid callback", "Missing authorization code or mailbox.")

    from google_auth_oauthlib.flow import Flow

    redirect_uri = _callback_url(request)
    flow = Flow.from_client_config(
        _client_config(redirect_uri), scopes=[GMAIL_SCOPE], redirect_uri=redirect_uri
    )
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Gmail token exchange failed")
        return _page("Authorization failed", f"Token exchange error: {exc}")

    creds = flow.credentials
    mailbox.oauth_payload = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or [GMAIL_SCOPE]),
    }
    mailbox.save(update_fields=["oauth_payload", "updated_at"])
    return _page("Gmail connected ✓",
                 f"{mailbox.email_address} is now authorized. Go to the panel and click "
                 f"<b>Fetch emails</b> to pull mail into tickets.")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def gmail_fetch(request):
    """Pull recent mail for a mailbox into tickets now. Uses IMAP or the Gmail API
    depending on settings.EMAIL_PROVIDER."""
    mailbox_id = request.query_params.get("mailbox") or request.data.get("mailbox")
    mailbox = Mailbox.objects.filter(pk=mailbox_id).first() if mailbox_id else \
        Mailbox.objects.first()
    if mailbox is None:
        return Response({"detail": "No mailbox."}, status=status.HTTP_404_NOT_FOUND)

    provider = getattr(settings, "EMAIL_PROVIDER", "imap")
    try:
        if provider == "imap":
            if not settings.IMAP_HOST or not settings.IMAP_USER:
                return Response(
                    {"detail": "IMAP not configured. Set IMAP_HOST/IMAP_USER/"
                               "IMAP_PASSWORD in backend/.env."},
                    status=status.HTTP_409_CONFLICT,
                )
            results = service.fetch_imap(mailbox)
        else:
            if not mailbox.oauth_payload:
                return Response(
                    {"detail": "Mailbox not connected. Click Connect Gmail first."},
                    status=status.HTTP_409_CONFLICT,
                )
            results = service.sync_history(mailbox)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Email fetch failed")
        return Response({"detail": f"Fetch failed: {exc}"},
                        status=status.HTTP_502_BAD_GATEWAY)
    # Only freshly-created tickets/messages count as "new" (UID/Message-ID dedup
    # means re-fetching never double-counts old mail).
    new_count = sum(1 for _t, _m, created in results if created)
    return Response({
        "mailbox": mailbox.email_address, "provider": provider,
        "fetched": new_count, "new": new_count,
        "ingested": new_count,  # back-compat
    })
