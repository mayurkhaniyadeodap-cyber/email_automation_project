"""
Public customer ticket tracking portal:  GET/POST /t?id=<hash>   (+ /t/file media)

Resolves a ticket by its tracking hash (internal-fallback tickets) or Care Panel
ticket id, and renders a full support-portal page from REAL data only -- Ticket,
Message, Attachment and AuditLogEntry. The customer can also post a reply with files.

No auth: the unguessable hash is the capability. Media is served scoped to the
resolved ticket so one hash can't read another ticket's files.
"""

import logging

from django.http import FileResponse, HttpResponse, HttpResponseRedirect
from django.shortcuts import render

from apps.tickets.models import Attachment, AuditLogEntry, Message, Ticket

logger = logging.getLogger(__name__)

# Visual progress ladder (the customer-facing stages).
STAGES = ["Created", "Evidence Received", "Awaiting Agent", "In Progress", "Resolved"]

# Map a ticket status to how far along the ladder it is.
_STATUS_STAGE = {
    Ticket.STATUS_NEW: 0,
    Ticket.STATUS_CLASSIFIED: 0,
    Ticket.STATUS_AWAITING_EVIDENCE: 0,
    Ticket.STATUS_AWAITING_AGENT: 2,
    Ticket.STATUS_ESCALATED: 2,
    Ticket.STATUS_IN_PROGRESS: 3,
    Ticket.STATUS_RESOLVED: 4,
    Ticket.STATUS_CLOSED: 4,
    Ticket.STATUS_AUTO_RESOLVED: 4,
}

# Bootstrap badge colour per status.
_STATUS_BADGE = {
    Ticket.STATUS_NEW: "secondary", Ticket.STATUS_CLASSIFIED: "secondary",
    Ticket.STATUS_AWAITING_EVIDENCE: "warning", Ticket.STATUS_AWAITING_AGENT: "primary",
    Ticket.STATUS_ESCALATED: "danger", Ticket.STATUS_IN_PROGRESS: "info",
    Ticket.STATUS_RESOLVED: "success", Ticket.STATUS_CLOSED: "dark",
    Ticket.STATUS_AUTO_RESOLVED: "success",
}

# Audit events worth showing on the timeline, with (sender, label).
_TIMELINE_EVENTS = {
    "ticket_created": ("System", "Support ticket created"),
    "attachment_received": ("Customer", "Customer uploaded photo / video"),
    "evidence_received": ("Customer", "Evidence received"),
    "care_panel_stored": ("System", "Ticket registered with the support team"),
    "ticket_updated": ("System", "Ticket updated"),
    "agent_reply_forwarded": ("Agent", "Agent replied"),
    "status_mirrored": ("Agent", None),       # label built from detail
    "internal_note": ("System", None),        # label = the note text
    "internal_tracking_generated": ("System", "Tracking link generated"),
}


def _resolve_ticket(hash_id):
    if not hash_id:
        return None
    ticket = Ticket.objects.filter(extracted__tracking_hash=hash_id).first()
    if ticket is None:
        ticket = Ticket.objects.filter(extracted__care_panel_ticket_id=hash_id).first()
        if ticket is not None and not (ticket.extracted or {}).get("tracking_hash"):
            logger.warning("TRACKING_HASH_MISSING ticket=%s id=%s -> backfilling",
                           ticket.ticket_id, hash_id)
            ticket.extracted = {**(ticket.extracted or {}), "tracking_hash": hash_id}
            ticket.save(update_fields=["extracted", "updated_at"])
    return ticket


def _media_kind(att):
    from apps.ingestion import evidence

    if evidence.is_video(att.filename, att.content_type):
        return "video"
    if evidence.is_photo(att.filename, att.content_type):
        return "image"
    return "file"


def _file_url(hash_id, att_id):
    """Scoped media URL, prefixed with the app's script name so it resolves under a sub-path
    deploy (e.g. /email_automation/t/file) as well as at the root."""
    from django.conf import settings

    prefix = (getattr(settings, "FORCE_SCRIPT_NAME", "") or "").rstrip("/")
    return f"{prefix}/t/file?id={hash_id}&a={att_id}"


def _build_media(ticket, hash_id):
    items = []
    for att in ticket.attachments.all().order_by("created_at"):
        items.append({
            "id": att.id,
            "filename": att.filename,
            "kind": _media_kind(att),
            "content_type": att.content_type or "application/octet-stream",
            "url": _file_url(hash_id, att.id),
        })
    return items


def _build_timeline(ticket):
    """Merge conversation Messages + milestone AuditLogEntry events, chronological."""
    events = []
    for m in ticket.messages.all().order_by("created_at"):
        if not (m.body_text or "").strip():
            continue
        sender = "Customer" if m.direction == Message.DIRECTION_INBOUND else "Support"
        events.append({
            "when": m.sent_at or m.created_at, "sender": sender,
            "text": m.body_text.strip(), "is_customer": sender == "Customer",
            "is_event": False,
        })
    for a in ticket.audit_log.all().order_by("created_at"):
        meta = _TIMELINE_EVENTS.get(a.event)
        if not meta:
            continue
        who, label = meta
        detail = a.detail or {}
        if a.event == "status_mirrored":
            label = f"Status changed to {str(detail.get('to', '')).replace('_', ' ').title()}"
        elif a.event == "internal_note":
            label = detail.get("note") or "Note added"
        events.append({
            "when": a.created_at, "sender": who, "text": label,
            "is_customer": who == "Customer", "is_event": True,
        })
    events.sort(key=lambda e: e["when"])
    return events


def _verified_customer_name(ticket):
    """The Shopify-VERIFIED order-owner name for a customer message, else 'Unknown'. NEVER the
    Gmail sender display name / From header / alias (matches serializers._owner_name)."""
    ex = ticket.extracted or {}
    if ex.get("customer_name") and ex.get("customer_name_source") == "shopify_verified":
        return ex["customer_name"]
    return "Unknown"


def _build_conversation(ticket, hash_id):
    """The COMPLETE email conversation (every non-draft inbound + outbound message) in
    chronological order. Each entry carries the sender name, sender type (Customer / DeoDap
    Support), email address, subject, timestamp, body and its own attachments. Additive: this
    does NOT touch the milestone timeline. New replies (customer or support) appear automatically
    because it is rebuilt from ticket.messages on every load."""
    cust_name = _verified_customer_name(ticket)     # Shopify-verified name, else 'Unknown'
    convo = []
    for m in ticket.messages.all().order_by("created_at"):
        if m.is_draft:
            continue                               # unsent drafts are internal-only
        inbound = m.direction == Message.DIRECTION_INBOUND
        atts = [{
            "filename": att.filename, "kind": _media_kind(att),
            "content_type": att.content_type or "application/octet-stream",
            "url": _file_url(hash_id, att.id),
        } for att in m.stored_attachments.all().order_by("created_at")]
        convo.append({
            "sender_name": cust_name if inbound else "DeoDap Support",
            "sender_type": "Customer" if inbound else "DeoDap Support",
            "is_customer": inbound,
            "email": m.from_email or "",
            "subject": (m.subject or "").strip(),
            "when": m.sent_at or m.created_at,
            "body": (m.body_text or "").strip(),
            "attachments": atts,
        })
    return convo


def _build_progress(ticket):
    idx = _STATUS_STAGE.get(ticket.status, 0)
    extracted = ticket.extracted or {}
    if (extracted.get("has_photo") or extracted.get("has_unboxing_video")
            or ticket.attachments.exists()):
        idx = max(idx, 1)
    return [{"name": s, "done": i < idx, "current": i == idx}
            for i, s in enumerate(STAGES)]


def _customer(ticket):
    extracted = ticket.extracted or {}
    name = extracted.get("name") or (ticket.customer_email or "Customer").split("@")[0]
    return {
        "name": name, "email": ticket.customer_email or "",
        "phone": extracted.get("phone") or "", "order_id": extracted.get("order_id") or "",
    }


def _handle_reply(request, ticket, hash_id):
    """Customer posts a reply (comment + files) from the portal."""
    from apps.ingestion import service

    comment = (request.POST.get("comment") or "").strip()
    files = request.FILES.getlist("files")
    if not comment and not files:
        return HttpResponseRedirect(f"/t?id={hash_id}")

    msg = Message.objects.create(
        ticket=ticket, direction=Message.DIRECTION_INBOUND,
        from_email=ticket.customer_email, subject=f"Re: {ticket.subject}",
        body_text=comment or "(no message)",
    )
    if files:
        blobs = [{"filename": f.name, "content": f.read(),
                  "mime_type": getattr(f, "content_type", "") or ""} for f in files]
        service._store_attachments(ticket, msg, blobs)
    AuditLogEntry.objects.create(
        ticket=ticket, actor="customer", event="portal_reply",
        detail={"chars": len(comment), "files": len(files)})
    logger.info("PORTAL-REPLY ticket=%s chars=%d files=%d",
                ticket.ticket_id, len(comment), len(files))
    try:
        service.process_existing_reply(ticket)   # promote / decide / confirm as usual
    except Exception:  # noqa: BLE001 -- the portal reply is still recorded
        logger.exception("process_existing_reply failed for %s", ticket.ticket_id)
    return HttpResponseRedirect(f"/t?id={hash_id}&sent=1")


def tracking_page(request):
    hash_id = (request.GET.get("id") or request.POST.get("id") or "").strip()
    ticket = _resolve_ticket(hash_id)
    if ticket is None:
        logger.warning("TRACKING_PAGE_404 id=%s", hash_id or "(empty)")
        return HttpResponse(_NOT_FOUND, status=404, content_type="text/html")

    if request.method == "POST":
        return _handle_reply(request, ticket, hash_id)

    logger.info("TRACKING_PAGE_OPENED ticket=%s id=%s status=%s",
                ticket.ticket_id, hash_id, ticket.status)
    ctx = {
        "ticket": ticket,
        "hash_id": hash_id,
        "number": ticket.ticket_number or ticket.ticket_id,
        "status_label": ticket.get_status_display(),
        "status_code": ticket.status,
        "status_badge": _STATUS_BADGE.get(ticket.status, "secondary"),
        "category": ticket.category or (ticket.category_ref.name if ticket.category_ref
                                        else "General"),
        "issue": ticket.issue_summary or ticket.subject or "-",
        "customer": _customer(ticket),
        "timeline": _build_timeline(ticket),
        "conversation": _build_conversation(ticket, hash_id),
        "media": _build_media(ticket, hash_id),
        "progress": _build_progress(ticket),
        "sent": request.GET.get("sent") == "1",
    }
    return render(request, "tracking/ticket.html", ctx)


def tracking_file(request):
    """Serve one attachment inline, scoped to the ticket resolved by ?id=<hash>."""
    ticket = _resolve_ticket((request.GET.get("id") or "").strip())
    if ticket is None:
        return HttpResponse(status=404)
    att = Attachment.objects.filter(id=request.GET.get("a") or 0, ticket=ticket).first()
    if att is None or not att.file:
        return HttpResponse(status=404)
    try:
        return FileResponse(att.file.open("rb"),
                            content_type=att.content_type or "application/octet-stream")
    except FileNotFoundError:
        return HttpResponse(status=404)


_NOT_FOUND = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Not found</title></head>"
    "<body style='font-family:Arial;text-align:center;margin-top:80px;color:#999'>"
    "<h1 style='font-size:48px;margin:0'>404</h1>"
    "<p>We couldn't find a ticket for this link.</p></body></html>"
)
