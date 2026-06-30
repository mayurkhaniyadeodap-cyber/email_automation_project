"""
Gmail push webhook (doc section 2). Cloud Pub/Sub delivers a POST here whenever a
new mail lands in a watched mailbox. The push body carries the mailbox address +
the new historyId; we look up the mailbox and pull everything since our stored
historyId.

This endpoint is public (Pub/Sub can't send our DRF token), so it is guarded by a
shared-secret token in the URL query string (settings.GMAIL_PUBSUB_TOKEN) -- set it
to the same value on the Pub/Sub push subscription.
"""

import base64
import binascii
import json
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.organizations.models import Mailbox

from . import service

logger = logging.getLogger(__name__)


def _decode_pubsub_envelope(payload):
    """Pull {emailAddress, historyId} out of the Pub/Sub push envelope."""
    message = (payload or {}).get("message", {})
    data = message.get("data")
    if not data:
        return None
    try:
        decoded = base64.b64decode(data).decode("utf-8")
        return json.loads(decoded)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def gmail_webhook(request):
    expected = getattr(settings, "GMAIL_PUBSUB_TOKEN", "")
    if expected and request.query_params.get("token") != expected:
        return Response({"detail": "forbidden"}, status=status.HTTP_403_FORBIDDEN)

    notification = _decode_pubsub_envelope(request.data)
    if not notification:
        # Ack malformed pushes with 204 so Pub/Sub stops retrying them.
        logger.warning("Gmail webhook: undecodable Pub/Sub envelope.")
        return Response(status=status.HTTP_204_NO_CONTENT)

    email_address = notification.get("emailAddress", "")
    history_id = notification.get("historyId")
    mailbox = Mailbox.objects.filter(
        email_address=email_address, is_active=True
    ).first()
    if not mailbox:
        logger.warning("Gmail webhook: no active mailbox for %s.", email_address)
        return Response(status=status.HTTP_204_NO_CONTENT)

    try:
        results = service.sync_history(mailbox, new_history_id=history_id)
    except Exception:  # noqa: BLE001 -- never 500 to Pub/Sub or it floods retries
        logger.exception("Gmail webhook: sync_history failed for %s.", email_address)
        return Response(status=status.HTTP_204_NO_CONTENT)

    return Response(
        {"mailbox": email_address, "ingested": len(results)},
        status=status.HTTP_200_OK,
    )
