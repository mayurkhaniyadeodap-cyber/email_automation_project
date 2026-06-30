"""
Care Panel -> Mail Engine webhook (DeoDap Care — Final Mail Flow v2.0, §6 row 4).

When an agent replies or changes a ticket's status inside the Care Panel, the panel
POSTs here. The Mail Engine then mails the customer the agent's message and mirrors
the new status locally -- so the Care Panel never has to touch the mailbox itself.

Public endpoint (the panel can't carry a DRF token), guarded by a shared secret:
either header  X-Care-Panel-Token: <settings.CARE_PANEL_WEBHOOK_TOKEN>  or  ?token=.
A blank CARE_PANEL_WEBHOOK_TOKEN disables the check (dev only).

Accepted payload (lenient on key names):
    {"ticket_id" | "ticket_number" | "hash" | "care_panel_ticket_id": "...",
     "status": "in_progress | resolved | closed | escalated",   (optional)
     "agent_message": "text to send the customer",              (optional)
     "agent_name": "..." }                                       (optional)
"""

import logging

from django.conf import settings
from django.utils import timezone
from rest_framework import status as http
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.tickets.models import AuditLogEntry, Message, Ticket

logger = logging.getLogger(__name__)

# External status string -> internal Ticket status. Keys are matched case-insensitively
# with spaces/hyphens normalized to underscores.
STATUS_MAP = {
    "in_progress": Ticket.STATUS_IN_PROGRESS,
    "in_process": Ticket.STATUS_IN_PROGRESS,
    "processing": Ticket.STATUS_IN_PROGRESS,
    "open": Ticket.STATUS_IN_PROGRESS,
    "resolved": Ticket.STATUS_RESOLVED,
    "completed": Ticket.STATUS_RESOLVED,
    "closed": Ticket.STATUS_CLOSED,
    "escalated": Ticket.STATUS_ESCALATED,
}


def _auth_ok(request):
    expected = getattr(settings, "CARE_PANEL_WEBHOOK_TOKEN", "")
    if not expected:
        return True
    got = (request.headers.get("X-Care-Panel-Token")
           or request.query_params.get("token") or "")
    return got == expected


def _find_ticket(payload):
    """Locate the ticket the panel is referring to (by our id, number, or hash)."""
    tid = str(payload.get("ticket_id") or "").strip()
    if tid:
        t = Ticket.objects.filter(ticket_id=tid).first()
        if t:
            return t
    number = str(payload.get("ticket_number") or "").lstrip("#").strip()
    if number:
        t = Ticket.objects.filter(ticket_number=number).first()
        if t:
            return t
    hash_id = str(payload.get("hash") or payload.get("care_panel_ticket_id") or "").strip()
    if hash_id:
        return Ticket.objects.filter(extracted__care_panel_ticket_id=hash_id).first()
    return None


def _normalize_status(value):
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return STATUS_MAP.get(key)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def care_panel_webhook(request):
    if not _auth_ok(request):
        return Response({"detail": "forbidden"}, status=http.HTTP_403_FORBIDDEN)

    payload = request.data or {}
    ticket = _find_ticket(payload)
    if ticket is None:
        logger.warning("Care Panel webhook: no ticket for %s", {
            k: payload.get(k) for k in ("ticket_id", "ticket_number", "hash")})
        return Response({"detail": "ticket not found"}, status=http.HTTP_404_NOT_FOUND)

    actions = []

    # 1) Mirror the status the agent set in the panel.
    new_status = _normalize_status(payload.get("status"))
    if new_status and new_status != ticket.status:
        old = ticket.status
        ticket.status = new_status
        if new_status in (Ticket.STATUS_RESOLVED, Ticket.STATUS_CLOSED) and not ticket.resolved_at:
            ticket.resolved_at = timezone.now()
        ticket.save(update_fields=["status", "resolved_at", "updated_at"])
        AuditLogEntry.objects.create(
            ticket=ticket, actor="care_panel", event="status_mirrored",
            detail={"from": old, "to": new_status})
        actions.append("status")

    # 2) Forward the agent's reply to the customer (panel never mails directly).
    agent_message = (payload.get("agent_message") or payload.get("message") or "").strip()
    if agent_message and ticket.customer_email:
        _forward_agent_reply(ticket, agent_message, payload.get("agent_name") or "agent")
        actions.append("reply")

    logger.info("CARE-PANEL-WEBHOOK ticket=%s actions=%s status=%s",
                ticket.ticket_id, actions, ticket.status)
    return Response({"ticket": ticket.ticket_id, "applied": actions}, status=http.HTTP_200_OK)


def _forward_agent_reply(ticket, text, agent_name):
    """Send the agent's panel message to the customer as an outbound mail + audit."""
    from apps.ingestion import service

    subject = f"Re: {ticket.subject}" if ticket.subject else "Update on your DeoDap request"
    message = Message.objects.create(
        ticket=ticket, direction=Message.DIRECTION_OUTBOUND,
        from_email=ticket.mailbox.email_address if ticket.mailbox else "",
        to_email=ticket.customer_email, subject=subject,
        body_text=text, sent_at=timezone.now(),
    )
    try:
        service.send_reply(message)
    except Exception:  # noqa: BLE001 -- delivery is best-effort
        logger.exception("Agent-reply forward failed for %s", ticket.ticket_id)
    AuditLogEntry.objects.create(
        ticket=ticket, actor="care_panel", event="agent_reply_forwarded",
        detail={"agent": agent_name, "chars": len(text)})
