"""
Ingestion service -- turns a Gmail message into a Ticket + Message (doc section 2).

The orchestration here is deliberately Gmail-agnostic: `ingest_message` takes a
*normalized* dict (see normalize.parse_gmail_message) and does the dedup +
threading + ignore-gate work. `sync_history` is the only piece that talks to the
Gmail client, and the client is injectable so the whole flow is testable offline.

Dedup:    on the Gmail internal message id (Message.gmail_message_id, unique).
Threading: on Gmail threadId -- a reply joins the existing ticket instead of
           opening a new one (doc section 2, step 4).
"""

import logging
import re

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.tickets.models import (
    Attachment,
    AuditLogEntry,
    Message,
    PendingConversation,
    Ticket,
)

from . import evidence, ignore_gate, mails

logger = logging.getLogger(__name__)


def _order_ref(pending):
    """A human reference to the order for templated mails (falls back gracefully)."""
    return pending.order_id or "your order"


def _complaint_ref(order_id, language="en"):
    """The localized 'complaint' clause for an evidence-request mail (M2 / M2P).

    With an order number it reads 'the complaint for order <N>'; WITHOUT one it reads
    'your complaint' -- so we never emit 'order your order', 'order order', a dangling
    'order ' with no number, or a duplicated placeholder."""
    oid = str(order_id or "").strip()
    clauses = {
        "en": (f"the complaint for order {oid}", "your complaint"),
        "hi": (f"ऑर्डर {oid} की शिकायत", "अपनी शिकायत"),
        "gu": (f"ઓર્ડર {oid} ની ફરિયાદ", "તમારી ફરિયાદ"),
    }
    with_order, without_order = clauses.get(language, clauses["en"])
    return with_order if oid else without_order


def _store_attachments(ticket, msg, blobs):
    """Persist email attachment bytes as Attachment rows (files on disk), record
    audit events, and flag photo/video evidence on the ticket."""
    from django.core.files.base import ContentFile

    import hashlib

    saved, has_photo, has_video = [], False, False
    for blob in blobs:
        content = blob.get("content")
        if not content:
            continue
        ct = (blob.get("mime_type") or "").lower()
        att = Attachment(
            ticket=ticket, message=msg,
            filename=blob.get("filename") or "attachment",
            content_type=blob.get("mime_type") or "", size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        att.file.save(att.filename, ContentFile(content), save=False)
        att.save()
        saved.append(att.filename)
        # Detect by MIME *or* filename extension -- an octet-stream .jpg/.mp4 still counts.
        has_photo = has_photo or evidence.is_photo(att.filename, ct)
        has_video = has_video or evidence.is_video(att.filename, ct)

    if not saved:
        return
    AuditLogEntry.objects.create(
        ticket=ticket, actor="system", event="attachment_received",
        detail={"files": saved, "count": len(saved)},
    )
    if has_photo or has_video:
        # Evidence arrived -> update the ticket's evidence flags so the engine can
        # move it forward (e.g. out of Awaiting Evidence).
        extracted = dict(ticket.extracted or {})
        if has_photo:
            extracted["has_photo"] = True
        if has_video:
            extracted["has_unboxing_video"] = True
        ticket.extracted = extracted
        ticket.save(update_fields=["extracted", "updated_at"])
        AuditLogEntry.objects.create(
            ticket=ticket, actor="system", event="evidence_received",
            detail={"has_photo": has_photo, "has_video": has_video},
        )


def build_client(mailbox):
    """Return a Gmail client for a mailbox. Patched in tests with a fake.

    Imported lazily so the rest of the engine (and the test suite) does not need
    the google-api-python-client packages installed.
    """
    from .gmail_client import GmailClient

    return GmailClient.for_mailbox(mailbox)


@transaction.atomic
def ingest_message(mailbox, message, *, run_ignore_gate=True):
    """Persist one normalized inbound mail. Idempotent on gmail_message_id.

    Returns (ticket, message_obj, created) where `created` is False if the mail
    was already ingested (a duplicate Pub/Sub push or fallback-poll overlap).
    """
    brand = mailbox.brand
    gmid = message.get("gmail_message_id") or ""

    if gmid and Message.objects.filter(gmail_message_id=gmid).exists():
        existing = Message.objects.get(gmail_message_id=gmid)
        # Re-fetching a known email: backfill its attachment files if we never
        # stored them (e.g. it was ingested before file-storage existed).
        blobs = message.get("attachment_blobs") or []
        if blobs and not existing.stored_attachments.exists():
            _store_attachments(existing.ticket, existing, blobs)
        return existing.ticket, existing, False

    thread_id = message.get("thread_id") or ""
    ticket = None
    if thread_id:
        ticket = Ticket.objects.filter(brand=brand, thread_id=thread_id).first()

    new_ticket = ticket is None
    if new_ticket:
        ticket = Ticket(
            organization=brand.organization,
            brand=brand,
            mailbox=mailbox,
            thread_id=thread_id,
            customer_email=message.get("from_email", ""),
            subject=message.get("subject", ""),
            status=Ticket.STATUS_NEW,
        )
        if run_ignore_gate:
            result = ignore_gate.evaluate(brand, message)
            if result.ignored:
                ticket.is_ignored = True
                ticket.ignored_reason = result.reason
                ticket.status = Ticket.STATUS_IGNORED
        ticket.save()

    try:
        with transaction.atomic():
            msg = Message.objects.create(
                ticket=ticket,
                direction=Message.DIRECTION_INBOUND,
                gmail_message_id=gmid or None,
                in_reply_to=message.get("in_reply_to", ""),
                references=message.get("references", []),
                from_email=message.get("from_email", ""),
                to_email=message.get("to", ""),
                subject=message.get("subject", ""),
                body_text=message.get("body_text", ""),
                body_html=message.get("body_html", ""),
                headers=message.get("headers", {}),
                attachments=message.get("attachments", []),
                imap_uid=message.get("imap_uid"),
            )
    except IntegrityError:
        # Lost a race on the unique gmail_message_id -- treat as duplicate.
        existing = Message.objects.get(gmail_message_id=gmid)
        return existing.ticket, existing, False

    _store_attachments(ticket, msg, message.get("attachment_blobs") or [])

    if new_ticket:
        if ticket.is_ignored:
            AuditLogEntry.objects.create(
                ticket=ticket, actor="system", event="ignored",
                detail={"reason": ticket.ignored_reason},
            )
        else:
            AuditLogEntry.objects.create(
                ticket=ticket, actor="system", event="ticket_created",
                detail={"thread_id": thread_id, "subject": ticket.subject},
            )
    else:
        # A new message on an EXISTING ticket = a reply / follow-up (an update).
        AuditLogEntry.objects.create(
            ticket=ticket, actor="system", event="ticket_updated",
            detail={"reason": "reply", "message_id": msg.id},
        )
    AuditLogEntry.objects.create(
        ticket=ticket, actor="system", event="message_received",
        detail={"message_id": msg.id, "gmail_message_id": gmid},
    )
    # Transient flag so the pipeline knows whether this was a brand-new ticket.
    ticket._created_now = new_ticket
    return ticket, msg, True


def sync_history(mailbox, new_history_id=None, client=None):
    """Pull mail that arrived since the mailbox's stored historyId (doc section 2).

    Used by both the Pub/Sub webhook and the fallback poll cron. Advances the
    mailbox's gmail_history_id only after a successful pass. Returns the list of
    (ticket, message, created) tuples for the messages it ingested.
    """
    client = client or build_client(mailbox)
    if client is None:
        logger.warning(
            "sync_history skipped: mailbox %s is not Gmail-authorized.",
            mailbox.email_address,
        )
        return []
    start = mailbox.gmail_history_id

    if start:
        message_ids = client.list_history(start_history_id=start)
    else:
        # No baseline yet (first sync): seed from the most recent inbox messages
        # so we don't try to replay all history from zero.
        message_ids = client.list_recent_message_ids()

    results = []
    for mid in message_ids:
        from .normalize import parse_gmail_message

        raw = client.get_message(mid)
        if not raw:
            continue
        normalized = parse_gmail_message(raw)
        # Classify-before-create + evidence-deferral pipeline (Smart Ticket Mgmt).
        ticket, msg, created = handle_incoming_email(mailbox, normalized)
        results.append((ticket, msg, created))

    target_history_id = new_history_id or client.latest_history_id()
    if target_history_id:
        mailbox.gmail_history_id = str(target_history_id)
        mailbox.save(update_fields=["gmail_history_id", "updated_at"])

    logger.info(
        "sync_history mailbox=%s ingested=%d new_history_id=%s",
        mailbox.email_address, len(results), target_history_id,
    )
    return results


def _auto_classify(ticket):
    """Run the AI classifier on a freshly ingested ticket, swallowing any failure
    so a classifier hiccup never blocks ingestion."""
    try:
        from apps.classifier import service as classifier

        classifier.classify_ticket(ticket)
    except Exception:  # noqa: BLE001 -- classification is best-effort here
        logger.exception("Auto-classify failed for ticket %s", ticket.ticket_id)


def _resolve_thread_id(brand, message):
    """Thread an IMAP mail by its In-Reply-To / References headers.

    If it replies to a Message-ID we've already stored, reuse that ticket's
    thread; otherwise the mail starts its own thread (keyed by its Message-ID).
    """
    candidates = []
    if message.get("in_reply_to"):
        candidates.append(message["in_reply_to"])
    candidates.extend(message.get("references") or [])
    for ref in candidates:
        prior = (
            Message.objects.filter(ticket__brand=brand, gmail_message_id=ref)
            .select_related("ticket")
            .first()
        )
        if prior:
            return prior.ticket.thread_id
    return message.get("message_id") or ""


def _validate_order_id(pending, message):
    """Order id for this conversation. The CURRENT reply WINS over the stored value so a
    customer who provides a new/corrected order number is honored (not re-checked against a
    stale one). Falls back to the pending's stored order id when the reply has none."""
    from apps.classifier.rule_classifier import _extract_order_id

    text = f"{message.get('subject', '')} {message.get('body_text', '')}"
    return _extract_order_id(text) or pending.order_id or ""


def _validate_phone(pending, message):
    """Customer phone for this conversation. The CURRENT reply WINS over the stored value --
    a customer who corrects their mobile must be verified against the NEW number, never the
    stale one (the 'could not verify' loop). Falls back to the stored phone when none given."""
    from apps.classifier.rule_classifier import _extract_phone

    text = f"{message.get('subject', '')} {message.get('body_text', '')}"
    return _extract_phone(text) or pending.phone or ""


# AWB / courier tracking number: an explicitly labelled "AWB: <ref>" value, else a standalone
# alphanumeric token that mixes letters AND digits (e.g. 7D132828320). A PURE-numeric value is
# intentionally NOT captured here -- it is read as the order id and separately re-checked against
# the courier -- so a bare number never masquerades as a verified AWB.
_AWB_LABEL_RE = re.compile(
    r"(?:awb|way\s*bill|tracking(?:\s*(?:no\.?|number|id))?)\s*[:#\-]?\s*([A-Za-z0-9]{6,20})",
    re.IGNORECASE)
_AWB_TOKEN_RE = re.compile(r"\b(?=[A-Za-z0-9]{8,20}\b)(?=\w*[A-Za-z])(?=\w*\d)[A-Za-z0-9]+\b")


def _extract_awb(text):
    """Return a courier AWB / tracking number found in `text`, or "" (see note above)."""
    m = _AWB_LABEL_RE.search(text or "")
    if m:
        return m.group(1)
    m = _AWB_TOKEN_RE.search(text or "")
    return m.group(0) if m else ""


def _has_identifier(pending):
    """A ticket can be created once we have ANY ONE customer identifier -- email,
    phone, OR order id. Phone is NOT mandatory (new rule): the Care Panel store may
    reject a phone-less ticket, but we then fall back to an internal tracking link
    rather than blocking ticket creation. order_id/phone are still collected when
    present (they enrich the Care Panel ticket) -- they just don't block."""
    return bool(pending.customer_email or pending.phone or pending.order_id)


def _pending_needs_order(pending):
    """True if this pending still needs the customer's ORDER before we can proceed.

    Applies to AUTO-REPLY (NO_TICKET) categories that must look the order up to answer --
    e.g. Shipment Tracking, whose sub-topic mandates `order_id`. For those, a reply that
    supplies only a phone/email is NOT enough: promoting it would (a) create a needless
    ticket and (b) consume the pending so a later order-reply spawns a SECOND ticket. So
    we keep the SAME pending open and re-ask for the order.

    Evidence/ticket categories (damaged / wrong / missing) are unaffected -- they still
    create on evidence + any identifier (the order is enriching, not blocking)."""
    if pending.order_id:
        return False
    sub = pending.sub_topic_ref
    if sub is None or "order_id" not in (getattr(sub, "mandatory_inputs", None) or []):
        return False
    from apps.decision import policy

    cat_code = getattr(pending.category_ref, "code", "") or (pending.category or "").split(".")[0]
    # Only gate auto-reply categories on the order; ticket categories must not be blocked.
    return not policy.requires_ticket(cat_code, getattr(sub, "name", ""),
                                      pending.issue_summary or "")


# Back-compat alias kept for tests that assert the video-mandatory wording (M2).
VIDEO_REQUEST_BODY = mails.MAILS["M2"]["en"][1]


def _auto_reply_subject(pending=None, *, category="", sub_topic=""):
    """Concern-based subject for an automated customer email (mails.subject_for), taken from the
    pending conversation's category + sub-topic (or explicit values). Falls back to
    'DeoDap Support | Support Request' for an unknown concern."""
    cat = category or (getattr(pending, "category", "") if pending else "") or ""
    sub = sub_topic or (getattr(pending, "sub_topic", "") if pending else "") or ""
    return mails.subject_for(cat, sub)


def _send_video_request(mailbox, message, pending):
    """M2: ask the customer for a VIDEO (mandatory for Defective / Missing / Wrong
    Item). Holds the conversation in 'waiting_for_video' -- no ticket, no Care Panel."""
    m2_subject, body = mails.render("M2", pending.language,
                            complaint_ref=_complaint_ref(pending.order_id, pending.language))
    subject = _auto_reply_subject(pending)
    refs = list(pending.references or [])
    if pending.original_message_id and pending.original_message_id not in refs:
        refs.append(pending.original_message_id)
    sent_id = _send_customer_email(
        pending.customer_email, subject, body,
        in_reply_to=pending.original_message_id, references=refs,
    )
    pending.evidence_requests = (pending.evidence_requests or 0) + 1
    pending.status = "waiting_for_video"
    if sent_id:
        pending.last_message_id = sent_id
    pending.save(update_fields=["evidence_requests", "status", "last_message_id", "updated_at"])


def _is_payment_pending(pending):
    """True when the held conversation is a Payment Issue (payment deducted but order not
    placed) -- it needs a PAYMENT SCREENSHOT, never a 'photo of the item'."""
    from apps.decision import policy

    cat_code = getattr(pending.category_ref, "code", "") or (pending.category or "").split(".")[0]
    blob = " ".join(filter(None, [pending.sub_topic or "", pending.issue_summary or "",
                                  pending.subject or "", pending.body_text or ""]))
    return str(cat_code).strip() == "8" or policy.payment_no_order(blob) \
        or "payment deducted but order not placed" in (pending.sub_topic or "").lower()


def _send_photo_request(mailbox, message, pending):
    """M2P: ask the customer for a PHOTO (Damaged / quality -- video optional). For a Payment
    Issue, ask for the PAYMENT SCREENSHOT (MPAY) instead. Holds the conversation in
    'awaiting_evidence' -- no ticket, no Care Panel call yet."""
    template = "MPAY" if _is_payment_pending(pending) else "M2P"
    m2p_subject, body = mails.render(template, pending.language,
                              complaint_ref=_complaint_ref(pending.order_id, pending.language))
    subject = _auto_reply_subject(pending)
    refs = list(pending.references or [])
    if pending.original_message_id and pending.original_message_id not in refs:
        refs.append(pending.original_message_id)
    sent_id = _send_customer_email(
        pending.customer_email, subject, body,
        in_reply_to=pending.original_message_id, references=refs,
    )
    pending.evidence_requests = (pending.evidence_requests or 0) + 1
    pending.status = "awaiting_evidence"
    if sent_id:
        pending.last_message_id = sent_id
    pending.save(update_fields=["evidence_requests", "status", "last_message_id", "updated_at"])


def _result_evidence_level(result, text=""):
    """Category-first evidence level for a fresh classification (none/photo/video)."""
    return evidence.evidence_level(
        category=result.category, sub_topic=result.sub_topic,
        issue_summary=result.issue_summary or "", text=text,
        category_ref=result.category_ref, sub_topic_ref=result.sub_topic_ref,
        ai_requires_evidence=getattr(result, "requires_evidence", False))


def _pending_evidence_level(pending):
    """Category-first evidence level for a held conversation (recomputed from its
    stored classification, so it stays consistent across replies)."""
    return evidence.evidence_level(
        category=pending.category, sub_topic=pending.sub_topic,
        issue_summary=pending.issue_summary or "", text=pending.body_text or "",
        category_ref=pending.category_ref, sub_topic_ref=pending.sub_topic_ref,
        ai_requires_evidence=pending.requires_evidence)


def _needs_evidence(result):
    """True when this category needs ANY photo/video evidence before a ticket."""
    return evidence.requires_evidence(_result_evidence_level(result))


def _message_has_evidence(message):
    """True if the email carries a photo/video attachment. Detection is by MIME *or*
    filename extension (evidence.is_photo / is_video) -- many clients send a valid
    .jpg/.mp4 as application/octet-stream, and a MIME-only check would miss it and
    wrongly re-ask for evidence."""
    for part in (message.get("attachment_blobs") or []) + (message.get("attachments") or []):
        fn, ct = part.get("filename") or "", part.get("mime_type") or ""
        if evidence.is_photo(fn, ct) or evidence.is_video(fn, ct):
            return True
    return False


def _message_has_video(message):
    """True if the email carries a VIDEO attachment (image-only returns False).
    By MIME *or* filename extension (a .mp4 sent as octet-stream still counts)."""
    for part in (message.get("attachment_blobs") or []) + (message.get("attachments") or []):
        if evidence.is_video(part.get("filename") or "", part.get("mime_type") or ""):
            return True
    return False


def _message_has_photo(message):
    """True if the email carries a PHOTO/image attachment (video-only returns False)."""
    for part in (message.get("attachment_blobs") or []) + (message.get("attachments") or []):
        if evidence.is_photo(part.get("filename") or "", part.get("mime_type") or ""):
            return True
    return False


# ---- Delivered-Item evidence cases (per-case request wording + validation) --------------
def _result_delivered_case(result, message):
    """The Delivered-Item evidence case for a fresh classification (or None)."""
    blob = " ".join(filter(None, [
        result.sub_topic or "", result.issue_summary or "",
        message.get("subject") or "", message.get("body_text") or ""]))
    return evidence.delivered_evidence_case(blob)


def _pending_delivered_case(pending):
    """The Delivered-Item evidence case for a held conversation (or None)."""
    blob = " ".join(filter(None, [
        pending.sub_topic or "", pending.issue_summary or "",
        pending.subject or "", pending.body_text or ""]))
    return evidence.delivered_evidence_case(blob)


def _message_meets_case(message, case):
    """True when THIS email already carries every file the case makes mandatory."""
    rule = evidence.DELIVERED_EVIDENCE_RULES[case]
    return ((not rule["photo"] or _message_has_photo(message))
            and (not rule["video"] or _message_has_video(message)))


def _pending_meets_case(pending, case):
    """True when the held conversation has accumulated every file the case makes mandatory."""
    rule = evidence.DELIVERED_EVIDENCE_RULES[case]
    return ((not rule["photo"] or pending.has_photo)
            and (not rule["video"] or pending.has_video))


def _send_delivered_evidence_request(mailbox, message, pending, case):
    """Send the Delivered-Item evidence request with the EXACT per-case wording and hold the
    conversation. Reuses the M2/M2P send mechanics (threading, evidence_requests counter); only
    the template + the waiting status differ. English wording per spec, regardless of language."""
    rule = evidence.DELIVERED_EVIDENCE_RULES[case]
    # Use the case-specific template's OWN subject + body (English). The template subject is the
    # precise per-concern one (e.g. Non-Working -> "... Troubleshooting"), which a keyword-based
    # subject_for cannot distinguish from Defective.
    subject, body = mails.render(rule["mail"], "en")
    refs = list(pending.references or [])
    if pending.original_message_id and pending.original_message_id not in refs:
        refs.append(pending.original_message_id)
    sent_id = _send_customer_email(
        pending.customer_email, subject, body,
        in_reply_to=pending.original_message_id, references=refs)
    pending.evidence_requests = (pending.evidence_requests or 0) + 1
    # A video-mandatory case waits in 'waiting_for_video'; a photo-only case in 'awaiting_evidence'
    # (same states the generic M2 / M2P flow uses, so reminders / auto-close behave identically).
    pending.status = "waiting_for_video" if rule["video"] else "awaiting_evidence"
    if sent_id:
        pending.last_message_id = sent_id
    pending.save(update_fields=["evidence_requests", "status", "last_message_id", "updated_at"])
    logger.info("DELIVERED-EVIDENCE-REQUEST pending=%s case=%s need_photo=%s need_video=%s "
                "status=%s", pending.id, case, rule["photo"], rule["video"], pending.status)


def _join_items(items):
    """Natural-language join for evidence item labels: 'the X' / 'the X and the Y'."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _send_progressive_evidence_request(mailbox, message, pending, case):
    """PROGRESSIVE evidence collection: acknowledge the evidence received so far and ask ONLY for the
    item(s) still missing -- NEVER re-send the full original EV_* template, and never re-request a
    file already received. Keeps the conversation held until every mandatory file arrives.

    Returns True when a 'still-missing' request was sent; False when nothing is missing (the caller
    then promotes the pending to a ticket immediately)."""
    missing = evidence.delivered_missing_items(
        case, has_photo=pending.has_photo, has_video=pending.has_video)
    if not missing:
        return False                                    # complete -> caller creates the ticket
    received = evidence.delivered_received_items(
        case, has_photo=pending.has_photo, has_video=pending.has_video)
    lines = []
    if received:
        lines.append(f"Thank you for sending the {_join_items(received)}.")
        lines.append("")
    lines.append("Please send the remaining required evidence:")
    lines.append("")
    lines.extend(f"• {item}" for item in missing)
    code = mails.normalize_lang(pending.language)
    body = "\n".join(lines) + f"\n\n{mails.SIGN[code]}"
    subject = _auto_reply_subject(pending)
    refs = list(pending.references or [])
    if pending.original_message_id and pending.original_message_id not in refs:
        refs.append(pending.original_message_id)
    sent_id = _send_customer_email(
        pending.customer_email, subject, body,
        in_reply_to=pending.original_message_id, references=refs)
    pending.evidence_requests = (pending.evidence_requests or 0) + 1
    # Still waiting: a video-mandatory case with no video yet stays 'waiting_for_video'.
    rule = evidence.DELIVERED_EVIDENCE_RULES[case]
    pending.status = ("waiting_for_video" if rule["video"] and not pending.has_video
                      else "awaiting_evidence")
    if sent_id:
        pending.last_message_id = sent_id
    pending.save(update_fields=["evidence_requests", "status", "last_message_id", "updated_at"])
    logger.info("PROGRESSIVE-EVIDENCE pending=%s case=%s received=%s missing=%s status=%s",
                pending.id, case, received, missing, pending.status)
    return True


def _result_requires_video(result):
    """A VIDEO is mandatory (photo insufficient) -- Defective / Missing / Wrong Item."""
    return evidence.requires_video(_result_evidence_level(result))


def _pending_requires_video(pending):
    return evidence.requires_video(_pending_evidence_level(pending))


def _attachment_counts(message):
    """Return (total, image_count, video_count) for an incoming message. Counts by MIME
    *or* filename extension, so an octet-stream .jpg/.mp4 is still counted as evidence."""
    parts = (message.get("attachment_blobs") or []) or (message.get("attachments") or [])
    images = videos = 0
    for p in parts:
        fn, ct = p.get("filename") or "", p.get("mime_type") or ""
        if evidence.is_video(fn, ct):
            videos += 1
        elif evidence.is_photo(fn, ct):
            images += 1
    return len(parts), images, videos


def _classify_dict(brand, message):
    """Classify a raw message dict (no ticket). Returns a ClassificationResult or None."""
    try:
        from apps.classifier import service as classifier

        return classifier.classify(brand, message)
    except Exception:  # noqa: BLE001 -- classification is best-effort
        logger.exception("Pre-ticket classify failed for %s", brand)
        return None


def _send_customer_email(to, subject, body, in_reply_to="", references=None, body_html=None,
                         attachments=None, from_email=None, reply_to=None):
    """Send a standalone email to the customer (used for the evidence request, which
    has no ticket yet). SMTP for the IMAP provider; no-op otherwise. When body_html is
    given the mail is multipart (HTML rendered, raw URLs hidden behind hyperlinks).
    `from_email` overrides the SMTP From (the agent's chosen sender / alias); `reply_to`
    forces replies back to the fetched inbox (defaults to the brand's primary inbox)."""
    from django.conf import settings

    if not to:
        logger.error("SMTP-SEND-FAILED reason=no_recipient subject=%r -> auto-reply NOT sent.",
                     subject)
        return None
    provider = getattr(settings, "EMAIL_PROVIDER", "imap")
    from_addr = (from_email or "").strip() or getattr(settings, "REPLY_FROM", "") \
        or settings.IMAP_USER or None
    reply_to = (reply_to or "").strip() or primary_inbox_address()
    logger.info("AUTO-REPLY-TO to=%s subject=%r provider=%s from=%s reply_to=%s in_reply_to=%s",
                to, subject, provider, from_addr or "-", reply_to or "-", in_reply_to or "-")
    if provider == "imap":
        try:
            from .smtp_client import send_email

            sent_id = send_email(
                to=to, subject=subject, body_text=body, body_html=body_html,
                from_addr=from_addr, reply_to=reply_to,
                in_reply_to=in_reply_to, references=references or [], attachments=attachments,
            )
            logger.info("AUTO-REPLY-DELIVERED to=%s message_id=%s", to, sent_id)
            # `in_reply_to` is the customer's original Message-ID this auto-reply answers -- log it
            # so a duplicate send would be visible against the same incoming id (idempotency audit).
            logger.info("AUTO_REPLY_SENT to=%s in_reply_to=%s reply_message_id=%s",
                        to, in_reply_to or "-", sent_id or "-")
            return sent_id
        except Exception as exc:  # noqa: BLE001 -- best-effort, but now LOUD + diagnosable
            logger.error("SMTP-SEND-FAILED to=%s subject=%r error=%r -> customer did NOT "
                         "receive the auto-reply.", to, subject, exc)
            return None
    logger.warning("AUTO-REPLY-SKIPPED provider=%s (not 'imap') -> no SMTP send for to=%s.",
                   provider, to)
    return None


def _pending_qs(brand):
    """Pending conversations that a reply may still join: open ones, plus auto-closed
    ones still inside the reopen window (a reply within REOPEN_DAYS reopens the case).
    A pending closed longer ago is excluded -> the reply starts a fresh case."""
    from datetime import timedelta

    from django.conf import settings

    cutoff = timezone.now() - timedelta(days=int(getattr(settings, "REOPEN_DAYS", 7)))
    return (
        PendingConversation.objects.filter(brand=brand)
        .exclude(Q(status="closed") & Q(closed_at__lt=cutoff))
    )


# Why an incoming email was (not) attached to a pending conversation.
PENDING_MATCH_REASONS = (
    "in_reply_to", "references", "ticket_reference", "no_match",
)
_TICKET_REF_RE = re.compile(r"\bTKT-\d{4}-\d{4,}\b", re.IGNORECASE)


def _match_pending(brand, message):
    """Return (pending_or_None, reason) for an incoming email. A message is attached to an
    existing pending conversation ONLY when it is genuinely the SAME THREAD -- by an
    explicit mail header (In-Reply-To / References) or a ticket reference. We deliberately do
    NOT match by sender email or by sender+subject: two separate emails with the same sender
    and the same subject (different Message-IDs, no thread headers) are DISTINCT conversations
    and must each start a new pending. `reason` is one of PENDING_MATCH_REASONS.

    Order of precedence:
      in_reply_to     -> the reply's In-Reply-To is the pending's original/last message id
      references      -> a References id is the pending's original/last message id
      ticket_reference-> an explicit TKT-id is present (a ticket reply, NOT a pending)
      no_match        -> no thread signal -> start a NEW conversation
    """
    qs = _pending_qs(brand)

    in_reply = (message.get("in_reply_to") or "").strip()
    if in_reply:
        p = qs.filter(Q(original_message_id=in_reply) | Q(last_message_id=in_reply)).first()
        if p:
            return p, "in_reply_to"

    refs = [r for r in (message.get("references") or []) if r]
    if refs:
        p = qs.filter(Q(original_message_id__in=refs) | Q(last_message_id__in=refs)).first()
        if p:
            return p, "references"

    # An explicit ticket reference belongs to a TICKET (handled by the thread-match path),
    # not a pending -> don't attach to a pending; surface the reason for the audit log.
    subject = message.get("subject", "") or ""
    if _TICKET_REF_RE.search(f"{subject} {message.get('body_text', '') or ''}"):
        return None, "ticket_reference"

    return None, "no_match"


def _find_pending(brand, message):
    """Thread-aware pending match (see _match_pending). Logs the match reason for audit;
    matches ONLY by In-Reply-To / References / ticket reference -- never by sender email or
    sender+subject, so a re-sent same-subject email always starts a new conversation."""
    pending, reason = _match_pending(brand, message)
    logger.info("PENDING-MATCH pending_match_reason=%s pending=%s from=%s subject=%r",
                reason, pending.id if pending else None,
                message.get("from_email"), (message.get("subject", "") or "")[:60])
    return pending


def _reopen_if_closed(pending):
    """A reply landed on an auto-closed pending within the reopen window -> revive it."""
    if pending.status == "closed":
        pending.status = ("waiting_for_video" if _pending_requires_video(pending)
                          and not pending.has_video else "awaiting_evidence")
        pending.closed_at = None
        pending.reminder_sent_at = None        # allow a fresh reminder cycle
        pending.save(update_fields=["status", "closed_at", "reminder_sent_at", "updated_at"])
        logger.info("PENDING-REOPENED id=%s -> %s", pending.id, pending.status)


def _store_pending_attachments(pending, message):
    """Store a reply's photo/video files on the pending conversation (so they aren't
    lost across replies). Returns (has_photo, has_video) for this message."""
    import hashlib

    from django.core.files.base import ContentFile

    photo = video = False
    seen = set(pending.attachments.values_list("sha256", flat=True))
    for blob in message.get("attachment_blobs") or []:
        content = blob.get("content")
        if not content:
            continue
        fn = blob.get("filename") or "attachment"
        ct = blob.get("mime_type") or ""
        is_p, is_v = evidence.is_photo(fn, ct), evidence.is_video(fn, ct)
        # Store ANY file recognized as photo/video by MIME *or* extension. A MIME-only
        # gate dropped a valid photo.jpg/video.mp4 that arrived as application/octet-stream,
        # so the extension re-scan below never saw it and evidence was never registered ->
        # the ticket was never created. Detect by extension too.
        if not (is_p or is_v):
            logger.info("PENDING-ATTACH-SKIP pending=%s file=%s mime=%s -> not photo/video, "
                        "not stored as evidence.", pending.id, fn, ct or "-")
            continue
        photo = photo or is_p
        video = video or is_v
        digest = hashlib.sha256(content).hexdigest()
        if digest in seen:
            continue
        att = Attachment(
            pending=pending, ticket=None, filename=fn,
            content_type=ct, size=len(content), sha256=digest,
        )
        att.file.save(att.filename, ContentFile(content), save=False)
        att.save()
        seen.add(digest)
        logger.info("PENDING-ATTACH-STORED pending=%s file=%s mime=%s photo=%s video=%s",
                    pending.id, fn, ct or "-", is_p, is_v)
    # Gmail-path metadata (no bytes) still tells us evidence type -- MIME or extension.
    for a in message.get("attachments") or []:
        fn, ct = a.get("filename") or "", a.get("mime_type") or ""
        photo = photo or evidence.is_photo(fn, ct)
        video = video or evidence.is_video(fn, ct)
    return photo, video


def _accumulate_pending(pending, message):
    """Fold a reply's evidence + order id into the pending conversation so we never
    re-ask for something already provided in an earlier reply.

    Evidence is satisfied by  pending.has_video OR current_reply_has_video  -- not
    only by attachments present in the current email.
    """
    previous_has_video = pending.has_video
    previous_has_photo = pending.has_photo
    total, images, videos = _attachment_counts(message)   # MIME-based counts

    photo, video = _store_pending_attachments(pending, message)
    # Robustness: re-scan ALL stored attachments for this conversation (by MIME +
    # extension) so an earlier evidence file is never missed -> never re-ask for
    # evidence already received.
    stored_photo, stored_video = evidence.scan_attachments(
        (a.filename, a.content_type) for a in pending.attachments.all())
    photo, video = photo or stored_photo, video or stored_video
    fields = []
    if (photo or video) and not pending.has_evidence:
        pending.has_evidence = True
        fields.append("has_evidence")
    if video and not pending.has_video:
        pending.has_video = True
        fields.append("has_video")
    if photo and not pending.has_photo:
        pending.has_photo = True
        fields.append("has_photo")
    # The latest reply WINS: a new/corrected order id or mobile replaces the stored one so we
    # never keep re-checking a stale (wrong) identifier the customer has already corrected.
    order_id = _validate_order_id(pending, message)
    if order_id and order_id != pending.order_id:
        pending.order_id = order_id
        fields.append("order_id")
    phone = _validate_phone(pending, message)
    if phone and phone != pending.phone:
        pending.phone = phone
        fields.append("phone")
    if order_id or phone:
        extracted = {**(pending.extracted or {})}
        if order_id:
            extracted["order_id"] = order_id
        if phone:
            extracted["phone"] = phone
        pending.extracted = extracted
        if "extracted" not in fields:
            fields.append("extracted")
    if fields:
        pending.save(update_fields=[*fields, "updated_at"])

    logger.info(
        "EVIDENCE-STATE pending=%s previous_has_video=%s previous_has_photo=%s "
        "current_reply_video_count=%d current_reply_image_count=%d "
        "effective_has_video=%s effective_has_evidence=%s order_id=%s phone=%s",
        pending.id, previous_has_video, previous_has_photo, videos, images,
        pending.has_video, pending.has_evidence, pending.order_id or "(none)",
        pending.phone or "(none)")


def _create_pending(mailbox, message, result, status="awaiting_evidence"):
    """Store the evidence-required email as a PendingConversation (no Ticket)."""
    brand = mailbox.brand
    extracted = dict(result.extracted or {})
    pending = PendingConversation.objects.create(
        organization=brand.organization, brand=brand, mailbox=mailbox,
        customer_email=message.get("from_email", ""),
        phone=extracted.get("phone") or "",
        order_id=extracted.get("order_id") or "",
        subject=message.get("subject", ""),
        original_message_id=message.get("message_id") or message.get("gmail_message_id") or "",
        last_message_id=message.get("message_id") or message.get("gmail_message_id") or "",
        thread_id=message.get("thread_id", ""),
        in_reply_to=message.get("in_reply_to", ""),
        references=message.get("references", []),
        headers=message.get("headers", {}),
        body_text=message.get("body_text", ""),
        body_html=message.get("body_html", ""),
        category=result.category, sub_topic=result.sub_topic,
        category_ref=result.category_ref, sub_topic_ref=result.sub_topic_ref,
        issue_summary=result.issue_summary, confidence=result.confidence,
        sentiment=result.sentiment, language=result.language,
        requires_agent=result.requires_agent, extracted=extracted, status=status,
        requires_evidence=_needs_evidence(result),
    )
    logger.info("EMAIL-SAVED table=tickets_pendingconversation id=%s status=%s "
                "thread_id=%s message_id=%s processed=yes hidden_from_inbox=yes "
                "(no Ticket row -> not in ticket inbox / Care Panel).",
                pending.id, pending.status, pending.thread_id or "-",
                pending.original_message_id or "-")
    return pending


def _send_identity_request(mailbox, message, pending):
    """M1: nothing identifying could be extracted or matched -> ask the customer for
    any one of order# / email / mobile / AWB. Holds the case in awaiting_evidence."""
    m1_subject, body = mails.render("M1", pending.language)
    subject = _auto_reply_subject(pending)
    refs = list(pending.references or [])
    if pending.original_message_id and pending.original_message_id not in refs:
        refs.append(pending.original_message_id)
    sent_id = _send_customer_email(
        pending.customer_email, subject, body,
        in_reply_to=pending.original_message_id, references=refs,
    )
    pending.evidence_requests = (pending.evidence_requests or 0) + 1
    pending.status = "awaiting_evidence"
    if sent_id:
        pending.last_message_id = sent_id
    pending.save(update_fields=["evidence_requests", "status", "last_message_id", "updated_at"])


def _is_cancellation(message, result):
    """Deterministic order-cancellation detection from the raw email + classification."""
    text = " ".join(filter(None, [
        message.get("subject", ""), message.get("body_text", ""),
        getattr(result, "issue_summary", "") or "",
    ]))
    return evidence.is_cancellation(text)


def _send_cancel_lookup(mailbox, message, pending, template="M_CANCEL_LOOKUP"):
    """Ask the customer for an order reference to cancel (M_CANCEL_LOOKUP), or -- when the
    identifier they sent could NOT be verified -- the 'not found, resend a valid one' message
    (M_CANCEL_NOT_FOUND). No evidence, NO ticket; the pending stays open for the next reply."""
    subject, body = mails.render(template, pending.language)
    subj = _auto_reply_subject(pending)
    refs = list(pending.references or [])
    if pending.original_message_id and pending.original_message_id not in refs:
        refs.append(pending.original_message_id)
    sent_id = _send_customer_email(
        pending.customer_email, subj, body,
        in_reply_to=pending.original_message_id, references=refs,
    )
    pending.evidence_requests = (pending.evidence_requests or 0) + 1
    if sent_id:
        pending.last_message_id = sent_id
    pending.save(update_fields=["evidence_requests", "last_message_id", "updated_at"])
    logger.info("CANCEL-%s sent to %s (order=%s)",
                "NOT-FOUND" if template == "M_CANCEL_NOT_FOUND" else "LOOKUP",
                pending.customer_email, pending.order_id or "(none)")


def _handle_cancellation(mailbox, message, result):
    """Order-cancellation flow: NEVER request photos/videos. Create the cancellation
    ticket if we already have an order reference, else ask for one (M_CANCEL_LOOKUP)."""
    from apps.classifier.rule_classifier import _extract_order_id, _extract_phone

    extracted = dict(result.extracted or {})
    # Capture phone / order from the raw email even if the AI didn't extract them.
    text = f"{message.get('subject', '')} {message.get('body_text', '')}"
    if not extracted.get("phone"):
        extracted["phone"] = _extract_phone(text) or ""
    if not extracted.get("order_id"):
        extracted["order_id"] = _extract_order_id(text) or ""
    extracted["intent"] = "ORDER_CANCELLATION"
    result.extracted = extracted
    result.requires_evidence = False
    order_id = extracted.get("order_id") or ""
    phone = extracted.get("phone") or ""
    _o, _p, email = _tracking_identifiers(
        message, exclude_emails=[mailbox.email_address, message.get("from_email")])
    awb_candidate = _extract_awb(text) or order_id or extracted.get("awb") or ""
    logger.info("CANCELLATION from=%s order=%s awb=%s phone=%s email=%s", message.get("from_email"),
                order_id or "-", awb_candidate or "-", phone or "-", email or "-")

    def _hold_and_ask(template="M_CANCEL_LOOKUP"):
        pending = _create_pending(mailbox, message, result)
        pending.requires_evidence = False
        pending.extracted = {**(pending.extracted or {}), "intent": "ORDER_CANCELLATION"}
        pending.save(update_fields=["requires_evidence", "extracted", "updated_at"])
        _send_cancel_lookup(mailbox, message, pending, template=template)
        return None, None, True

    # Cancellation identifiers are Order Number / AWB / Registered Email (NOT phone: a phone alone
    # doesn't say WHICH order to cancel, so we still ask for one). Phone is recorded, never used
    # to auto-create.
    if order_id or email or awb_candidate:
        # VERIFY the identifier BEFORE creating a ticket (same rule as the reply path).
        proceed, status, info, verified_awb = _verify_cancellation_identifier(
            mailbox.brand, order_id=order_id, email=email, awb=awb_candidate)
        if proceed:
            result.extracted = _stamp_verified_customer({**extracted}, info)
            if verified_awb and not result.extracted.get("awb"):
                result.extracted["awb"] = verified_awb
            ticket, msg, created = ingest_message(mailbox, message)
            if created and getattr(ticket, "_created_now", True):
                _finalize_new_ticket(ticket, result)
            return ticket, msg, created
        # Provided an identifier that did NOT verify -> hold, ask for a valid one. NO ticket.
        logger.warning("TICKET_CREATION_SKIPPED_INVALID_ORDER from=%s (first email) -- identifier "
                       "not verified; NO ticket, NO Care Panel, NO confirmation.",
                       message.get("from_email"))
        return _hold_and_ask(template="M_CANCEL_NOT_FOUND")

    return _hold_and_ask()      # nothing provided -> ask for an order reference


def _is_shipment_tracking(obj):
    """True if this pending/result is the Shipment & Delivery Tracking category (code 1)."""
    code = getattr(getattr(obj, "category_ref", None), "code", "") or ""
    if not code:
        code = (getattr(obj, "category", "") or "").split(".")[0].strip()
    return code == "1"


def _map_tracking_status(raw):
    """Present the LIVE courier/shipment status to the customer. lookup_tracking already ranks
    the courier/shipment status ABOVE Shopify fulfillment, so `raw` is the courier status when
    available. The ONLY transform is Return To Origin (any RTO variant -- 'RTO', 'RTO In Transit',
    'RTO Delivered', 'Return To Origin') -> 'Return To Origin (RTO)'. Every other status
    (In Transit / Out For Delivery / Delivered / Returned / Cancelled / NDR / ...) is verbatim."""
    raw = (raw or "").strip()
    s = re.sub(r"\s+", " ", raw.lower())
    if s.startswith("rto") or "return to origin" in s:
        return "Return To Origin (RTO)"
    return raw


def _tracking_status_text(info):
    """Customer-facing status = the resolved live courier/shipment status (info['raw_status'],
    which lookup_tracking prioritizes courier -> Care Panel -> ... -> Shopify fulfillment), run
    through _map_tracking_status so 'Return To Origin' shows as 'Return To Origin (RTO)' and
    Shopify 'fulfilled' never leaks."""
    return _map_tracking_status(info.get("raw_status") or info.get("status")) or "Update"


def _is_rto_status(info):
    return _tracking_status_text(info) == "Return To Origin (RTO)"


def _overall_shipment_status(shipments):
    """Overall status for a multi-package order (decision rules):
      all delivered            -> Delivered
      some delivered, some not  -> Partially Delivered
      none delivered (all moving) -> In Transit"""
    total = len(shipments)
    delivered = sum(1 for s in shipments if s.get("delivered"))
    if total and delivered == total:
        return "Delivered"
    if delivered >= 1:
        return "Partially Delivered"
    return "In Transit"


def _format_multi_shipment_details(info):
    """Status block for an order shipped in MULTIPLE packages: an overall status (per the decision
    rules) + EVERY shipment (Tracking / Courier / Status / DeoDap Track link). Every link is the
    DeoDap tracking page -- courier URLs are NEVER shown. If any package is still on the way we
    never report a blanket 'Order Delivered'."""
    shipments = info.get("shipments") or []
    lines = []
    if info.get("order_id"):
        lines.append(f"Order ID: {info['order_id']}")
    lines += [f"Overall Status: {_overall_shipment_status(shipments)}", ""]
    for i, s in enumerate(shipments, 1):
        status = _map_tracking_status(s.get("raw_status")) or "Update"
        lines.append(f"Shipment {i}")
        lines.append(f"Tracking: {s.get('awb') or '-'}")
        if s.get("courier"):
            lines.append(f"Courier: {s['courier']}")
        lines.append(f"Status: {status}")
        if s.get("tracking_url"):
            lines += ["Track Shipment:", s["tracking_url"]]
        lines.append("")
    delivered = sum(1 for s in shipments if s.get("delivered"))
    total = len(shipments)
    if delivered < total:
        # At least one package is still on the way -> never say "Order Delivered".
        lines.append("Your order has been shipped in multiple packages.")
        lines.append("")
        if delivered == 0:
            lines.append("All packages are still on the way.")
        else:
            remaining = total - delivered
            lines.append("One package has already been delivered." if delivered == 1
                         else f"{delivered} packages have already been delivered.")
            lines.append("The remaining package is still on the way." if remaining == 1
                         else "The remaining packages are still on the way.")
        lines.append("")
        lines.append("Please use the tracking links above to track each shipment.")
    else:
        lines.append("All packages have been delivered.")
    return "\n".join(lines).strip()


def _format_tracking_details(info):
    """Build the customer status block: Order ID / Status / (RTO note) / Courier / AWB / Refund /
    live courier URL. The tracking URL is the REAL Shopify/courier link -- never care.deodap.in
    and never build_tracking_url(). Status uses the mapped courier status (see above).

    A multi-package order (2+ tracking numbers) renders every shipment separately (see
    _format_multi_shipment_details) so a still-in-transit package is never hidden behind the
    first one's 'Delivered'."""
    if info.get("multi_shipment") and info.get("shipments"):
        return _format_multi_shipment_details(info)
    lines = []
    if info.get("order_id"):
        lines.append(f"Order ID: {info['order_id']}")
    lines.append(f"Status: {_tracking_status_text(info)}")
    rto = _is_rto_status(info)
    if rto:
        lines.append("Your shipment is currently being returned to the seller.")
    if info.get("courier"):
        lines.append(f"Courier: {info['courier']}")
    if info.get("awb"):
        lines.append(f"AWB: {info['awb']}")
    # Refund Status -- from the Shopify order's financial/refund data; for RTO, verification is
    # done after the returned shipment reaches the warehouse.
    refund = info.get("refund_status") or "Not Applicable"
    if rto and refund in ("Not Applicable", "", None):
        refund = "Pending verification after returned shipment reaches the warehouse."
    lines.append(f"Refund Status: {refund}")
    if info.get("tracking_url"):
        lines += ["", "Track Order:", info["tracking_url"]]
    return "\n".join(lines)


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _clean_reply(body):
    """Drop quoted thread history so we only read what the customer just typed (avoids
    scraping emails/numbers out of the quoted original mail)."""
    out = []
    for ln in (body or "").splitlines():
        s = ln.strip()
        if s.startswith((">", "On ", "-----Original", "From:", "Sent:", "________")):
            break
        if s.endswith("wrote:") and "@" in s:
            break
        out.append(ln)
    return "\n".join(out)


def _extract_email(text, exclude=()):
    """First email address in `text` that isn't in `exclude` (our mailbox / the sender)."""
    ex = {e.strip().lower() for e in exclude if e}
    for m in _EMAIL_RE.findall(text or ""):
        if m.lower() not in ex:
            return m
    return ""


def _tracking_identifiers(message, *, exclude_emails=()):
    """Extract the explicit identifiers a customer may have typed -- order id / phone /
    registered email -- from the message BODY only (the From address is never used)."""
    from apps.classifier.rule_classifier import _extract_order_id, _extract_phone

    subject = message.get("subject", "") or ""
    body = message.get("body_text", "") or ""
    combined = f"{subject} {body}"
    order_id = _extract_order_id(combined) or ""
    # Phone can appear in the subject too ("Re: order help 7004810519") -- search both.
    phone = _extract_phone(combined) or ""
    email = _extract_email(_clean_reply(body), exclude=exclude_emails)
    logger.info("RAW_EMAIL_BODY %r", body[:500])
    logger.info("EXTRACTED_ORDER_ID %s | EXTRACTED_PHONE %s | EXTRACTED_EMAIL %s",
                order_id or "-", phone or "-", email or "-")
    return order_id, phone, email


def _send_tracking_lookup(pending):
    """STEP 2: no identifier yet -> ask the customer for ANY ONE of Order Number / Mobile /
    Email (M_TRACK_LOOKUP). No Shopify call, no ticket, no link. Keeps the pending OPEN."""
    subject, body = mails.render("M_TRACK_LOOKUP", pending.language)
    subj = _auto_reply_subject(pending)
    refs = list(pending.references or [])
    if pending.original_message_id and pending.original_message_id not in refs:
        refs.append(pending.original_message_id)
    sent_id = _send_customer_email(pending.customer_email, subj, body,
                                   in_reply_to=pending.original_message_id, references=refs)
    pending.evidence_requests = (pending.evidence_requests or 0) + 1
    if sent_id:
        pending.last_message_id = sent_id
    pending.save(update_fields=["evidence_requests", "last_message_id", "updated_at"])
    logger.info("TRACKING-LOOKUP-ASK sent to %s (pending=%s) -- no Shopify call.",
                pending.customer_email, pending.id)


def _send_tracking_status(brand, *, to, language, order_id="", phone="", email="", awb="",
                          subject="", in_reply_to="", references=None, category="", sub_topic=""):
    """STEP 4-5-6: look the order up LIVE by ANY ONE identifier (order number / phone /
    registered email) and send the ACTUAL Shopify/courier status. Never a ticket, never a
    care.deodap.in link, never build_tracking_url(). Returns the lookup `info`."""
    from apps.integrations import context as live_context

    # Same identifiers, same lookup as every other workflow (see _shopify_verify). awb is an
    # extra enrichment key for the courier link -- it never changes the match predicate.
    logger.info("TRACKING-VERIFY extracted_order=%s extracted_mobile=%s extracted_email=%s",
                order_id or "-", phone or "-", email or "-")
    logger.info("SHOPIFY-LOOKUP workflow=tracking by=%s order=%s phone=%s email=%s",
                "order" if order_id else "phone" if phone else "email",
                order_id or "-", phone or "-", email or "-")
    info = live_context.lookup_tracking(brand, order_id=order_id, phone=phone,
                                        email=email, awb=awb)
    logger.info("SHOPIFY-LOOKUP-RESULT workflow=tracking configured=%s found=%s error=%s "
                "resolved_order=%s status=%s courier=%s awb=%s",
                info["configured"], info["found"], info["error"], info.get("order_id"),
                info["status"], info["courier"], info["awb"])

    if info["found"] and not info["error"]:
        logger.info("SHOPIFY-MATCH workflow=tracking order=%s status=%s -> "
                    "SKIP-VERIFICATION-EMAIL (sending live status).",
                    info.get("order_id"), info["status"])
        subj, body = mails.render("M_TRACK_STATUS", language,
                                  details=_format_tracking_details(info))
    elif info["configured"] and not info["error"]:
        logger.info("SHOPIFY-NO-MATCH workflow=tracking order=%s phone=%s email=%s -> "
                    "verification email (M_TRACK_NOT_FOUND).",
                    order_id or "-", phone or "-", email or "-")
        subj, body = mails.render("M_TRACK_NOT_FOUND", language)   # STEP 6
    else:
        subj, body = mails.render("M_TRACK_UNAVAILABLE", language)

    logger.info("TRACKING-EMAIL order_id=%s awb=%s tracking_url=%s",
                info.get("order_id") or "-", info.get("awb") or "-",
                info.get("tracking_url") or "-")
    # Concern-based subject (Shipment Tracking Information / Refund Status Update / ...). Defaults
    # to Shipment Tracking when the caller didn't pass a category/sub-topic (this IS the tracking
    # flow). `subject`/`subj` are kept for backward compatibility but no longer drive the subject.
    send_subject = _auto_reply_subject(category=category or "Shipment & Delivery Tracking",
                                       sub_topic=sub_topic or "Shipment Tracking")
    # HTML version: render the tracking link as a "View Order Status" hyperlink so the customer
    # never sees the raw URL (BUG 2). A multi-package order keeps EACH per-shipment link (they're
    # linkified individually), so we don't collapse them into one "View Order Status".
    html_url = "" if info.get("multi_shipment") else (info.get("tracking_url") or "")
    body_html = _email_html(body, html_url)
    info["sent_id"] = _send_customer_email(to, send_subject, body, body_html=body_html,
                                           in_reply_to=in_reply_to, references=references or [])
    return info


def _email_html(plain_body, tracking_url=""):
    """Build an HTML body from the localized plain-text body: escape it, turn newlines into
    <br>, and replace the raw tracking URL with a 'View Order Status' hyperlink so the
    customer never sees the full URL."""
    import html as _html

    esc = _html.escape(plain_body or "")
    if tracking_url:
        esc_url = _html.escape(tracking_url, quote=True)
        link = f'<a href="{esc_url}">View Order Status</a>'
        esc = esc.replace(_html.escape(tracking_url), link)
    # Linkify any remaining raw http(s) URLs (e.g. the per-shipment tracking links in a
    # multi-package order) so each one is clickable. URLs already wrapped in an <a> above are
    # preceded by a double-quote and skipped by the lookbehind.
    esc = re.sub(r'(?<!")(https?://[^\s<]+)',
                 lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', esc)
    return "<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">" \
           + esc.replace("\n", "<br>") + "</div>"


def _tracking_note(info):
    """One-line internal note summarising the tracking status auto-replied to the customer."""
    bits = [f"Order {info.get('order_id') or '-'}",
            f"status {_tracking_status_text(info)}"]
    if info.get("courier"):
        bits.append(f"courier {info['courier']}")
    if info.get("awb"):
        bits.append(f"AWB {info['awb']}")
    return "Shipment tracking auto-replied: " + ", ".join(bits)


def _record_tracking_ticket(mailbox, pending, message, info):
    """Create a local AUTO-RESOLVED ticket for a completed Shipment-Tracking auto-reply so
    it appears in the Tickets list (Route A semantics). NO external Care Panel store call
    and NO extra email -- the status was already sent by _send_tracking_status."""
    from apps.classifier.service import ClassificationResult, apply_to_ticket

    brand = mailbox.brand
    thread_id = pending.thread_id or pending.original_message_id or message.get("thread_id", "")
    ticket = Ticket.objects.create(
        organization=brand.organization, brand=brand, mailbox=mailbox,
        thread_id=thread_id, customer_email=pending.customer_email,
        subject=pending.subject, status=Ticket.STATUS_NEW,
    )
    AuditLogEntry.objects.create(
        ticket=ticket, actor="system", event="ticket_created",
        detail={"from_pending": pending.id, "thread_id": thread_id, "kind": "tracking_auto_reply"},
    )
    # Recreate the ORIGINAL inbound email (held in the pending conversation).
    if pending.original_message_id and not Message.objects.filter(
        gmail_message_id=pending.original_message_id
    ).exists():
        Message.objects.create(
            ticket=ticket, direction=Message.DIRECTION_INBOUND,
            gmail_message_id=pending.original_message_id,
            in_reply_to=pending.in_reply_to, references=pending.references,
            from_email=pending.customer_email, subject=pending.subject,
            body_text=pending.body_text, body_html=pending.body_html, headers=pending.headers,
        )
    # Re-apply the stored classification (no second AI call), then ingest the REPLY (persist
    # only -- run_ignore_gate=False and an existing thread_id means no pipeline re-entry).
    result = ClassificationResult(
        category=pending.category, sub_topic=pending.sub_topic,
        confidence=pending.confidence or 0.0, extracted=dict(pending.extracted or {}),
        sentiment=pending.sentiment, language=pending.language, is_support_request=True,
        issue_summary=pending.issue_summary, requires_evidence=False, requires_agent=False,
        category_ref=pending.category_ref, sub_topic_ref=pending.sub_topic_ref)
    apply_to_ticket(ticket, result, classification_status=Ticket.CLS_CLASSIFIED)
    reply = dict(message)
    reply["thread_id"] = thread_id
    reply.pop("attachment_blobs", None)
    ingest_message(mailbox, reply, run_ignore_gate=False)
    # Record the live tracking facts and mark AUTO-RESOLVED (local-only, no external store).
    ticket.refresh_from_db()
    extracted = {**(ticket.extracted or {})}
    for k in ("order_id", "awb", "courier", "tracking_url"):
        if info.get(k):
            extracted[k] = info[k]
    extracted["tracking_status"] = info.get("raw_status") or info.get("status") or ""
    _stamp_verified_customer(extracted, info)   # verified Shopify customer name
    ticket.extracted = extracted
    ticket.status = Ticket.STATUS_AUTO_RESOLVED
    ticket.ai_handled = True
    ticket.save()
    _add_internal_note(ticket, _tracking_note(info))
    logger.info("TRACKING-TICKET created %s (auto-resolved, no Care Panel) order=%s status=%s",
                ticket.ticket_id, info.get("order_id"), info.get("status"))
    return ticket


def _finalize_tracking_pending(pending, info, order_id, *, mailbox=None, message=None):
    """Close the pending once a tracking status was delivered (STEP 5); otherwise keep it
    OPEN (clearing a bad order) for a corrected identifier (STEP 6). Shared by the first-
    email and reply tracking handlers.

    On success, ALSO record a local auto-resolved ticket (so the tracking interaction shows
    in the Tickets list, like a Route A case) -- but never an external Care Panel store
    call and never a second email. Pass mailbox + message to enable that record."""
    if info.get("sent_id"):
        pending.last_message_id = info["sent_id"]
    if info["found"] and not info["error"]:
        if mailbox is not None and message is not None:
            _record_tracking_ticket(mailbox, pending, message, info)
        pending.status = "closed"
        pending.closed_at = timezone.now()
        pending.save(update_fields=["status", "closed_at", "last_message_id", "updated_at"])
        logger.info("TRACKING-STATUS sent order=%s status=%s -> pending closed.",
                    info.get("order_id"), info["status"])
    else:
        if order_id and info["configured"] and not info["error"] and not info["found"]:
            pending.order_id = ""
        pending.evidence_requests = (pending.evidence_requests or 0) + 1
        pending.save(update_fields=["order_id", "evidence_requests",
                                    "last_message_id", "updated_at"])


def _handle_tracking_first_email(mailbox, message, result):
    """Shipment Tracking, FIRST email (universal verification rule). If the email ALREADY
    contains a valid identifier (order number / registered mobile / registered email), we
    look it up and send the LIVE status immediately -- NO M_TRACK_LOOKUP ask. Otherwise we
    create a pending and ask for an identifier; the lookup then happens on the reply.

    Either path records this Message-ID (via the pending / the auto-resolved ticket) so a
    re-fetch can't re-send."""
    pending = _create_pending(mailbox, message, result)
    order_id, phone, email = _tracking_identifiers(
        message, exclude_emails=[mailbox.email_address, pending.customer_email])
    order_id = order_id or pending.order_id or ""
    phone = phone or pending.phone or ""
    if order_id or phone or email:
        logger.info("IDENTIFIER-DETECTED category=tracking pending=%s order=%s phone=%s "
                    "email=%s -> verifying on first email (no ask).", pending.id,
                    order_id or "-", phone or "-", email or "-")
        return _handle_tracking_pending(mailbox, message, pending)
    # No identifier -> ask (STEP 2). Clear any stray classifier-captured values.
    if pending.phone or pending.order_id:
        pending.phone = ""
        pending.order_id = ""
        pending.save(update_fields=["phone", "order_id", "updated_at"])
    _send_tracking_lookup(pending)
    return None, None, True


def _handle_tracking_pending(mailbox, message, pending):
    """Shipment Tracking reply on an existing pending (STEP 3-6). Look up by ANY identifier
    the customer now provided (order / phone / email); send live status and CLOSE the
    pending only once status is delivered. The SAME pending stays OPEN until a valid
    identifier is received. No ticket, no duplicate."""
    order_id, phone, email = _tracking_identifiers(
        message, exclude_emails=[mailbox.email_address, pending.customer_email])
    order_id = order_id or pending.order_id or ""
    phone = phone or pending.phone or ""
    if not (order_id or phone or email):
        _send_tracking_lookup(pending)               # still nothing -> ask again, keep open
        return None, None, True

    refs = list(pending.references or [])
    if pending.original_message_id and pending.original_message_id not in refs:
        refs.append(pending.original_message_id)
    info = _send_tracking_status(
        mailbox.brand, to=pending.customer_email, language=pending.language,
        order_id=order_id, phone=phone, email=email,
        awb=(pending.extracted or {}).get("awb") or "", subject=pending.subject,
        in_reply_to=pending.original_message_id, references=refs,
        category=pending.category, sub_topic=pending.sub_topic)
    _finalize_tracking_pending(pending, info, order_id, mailbox=mailbox, message=message)
    return None, None, True


# Category-specific confirmation sentence for a VERIFIED two-step inquiry ticket
# (rendered into M5_INQUIRY / M5_INQUIRY_N by send_confirmation).
_INQUIRY_REGISTERED_LINE = {
    "invoice": {
        "en": "Your invoice request has been verified and registered.",
        "hi": "आपका इनवॉइस अनुरोध सत्यापित और दर्ज कर लिया गया है।",
        "gu": "તમારી ઇન્વોઇસ વિનંતી ચકાસીને નોંધી લેવાઈ છે.",
    },
    "franchise": {
        "en": "Your franchise inquiry has been registered.",
        "hi": "आपकी फ्रेंचाइज़ी पूछताछ दर्ज कर ली गई है।",
        "gu": "તમારી ફ્રેન્ચાઇઝ પૂછપરછ નોંધી લેવાઈ છે.",
    },
    "dropship": {
        "en": "Your dropshipping inquiry has been registered.",
        "hi": "आपकी ड्रॉपशिपिंग पूछताछ दर्ज कर ली गई है।",
        "gu": "તમારી ડ્રોપશિપિંગ પૂછપરછ નોંધી લેવાઈ છે.",
    },
    "company": {
        "en": "Your company profile request has been registered.",
        "hi": "आपका कंपनी प्रोफ़ाइल अनुरोध दर्ज कर लिया गया है।",
        "gu": "તમારી કંપની પ્રોફાઇલ વિનંતી નોંધી લેવાઈ છે.",
    },
}


# Two-step verification inquiries: keyword -> kind. Detected on the FIRST email only.
_VERIFY_INQUIRY_KEYWORDS = [
    ("invoice", ("invoice", "tax invoice", "gst invoice", "bill copy", "copy of bill",
                 "need invoice", "send invoice", "want invoice")),
    ("franchise", ("franchise", "franchisee")),
    ("dropship", ("dropship", "drop ship", "drop-ship", "dropshipping")),
    ("company", ("company profile", "company details", "company information",
                 "about your company", "about deodap")),
]


def _verification_inquiry_kind(message):
    """Return 'invoice' / 'franchise' / 'dropship' / 'company' if the email is one of the
    two-step verification inquiries, else None."""
    text = f"{message.get('subject', '')} {message.get('body_text', '')}".lower()
    for kind, kws in _VERIFY_INQUIRY_KEYWORDS:
        if any(k in text for k in kws):
            return kind
    return None


# ======================================================================================= #
# DEDICATED INQUIRY WORKFLOW (Franchisee / Dropshipping / Company Profile / Invoice / Other)
# A multi-step conversational flow, entirely separate from support / verification. NEVER asks
# for order verification / registered email / AWB and NEVER creates a support ticket.
# ======================================================================================= #

def _is_cod_inquiry(message):
    """True when the email asks about Cash on Delivery / COD / pay-on-delivery / cash payment."""
    from apps.decision import policy

    blob = f"{message.get('subject','') or ''} {message.get('body_text') or message.get('snippet') or ''}"
    return policy.cod_inquiry(blob)


def _handle_cod_inquiry(mailbox, message, gmid):
    """CASH ON DELIVERY inquiry -> send the fixed 'online prepaid only' reply and mark the
    conversation Auto Resolved. NO ticket, NO pending, NO Care Panel, NO manual review, NO pincode.
    Duplicate auto-replies for the SAME conversation are prevented (the same-message re-fetch is
    already blocked by the ProcessedEmail claim; this also blocks a second COD reply on the thread)."""
    from apps.tickets.models import ProcessedEmail

    thread_id = message.get("thread_id") or gmid or ""
    to = message.get("from_email") or ""
    logger.info("COD_INQUIRY_DETECTED message_id=%s thread_id=%s from=%s", gmid or "-",
                thread_id or "-", to or "-")

    # Prevent duplicate auto-replies for the SAME conversation: if a message THIS one is replying
    # to already received a COD auto-reply, do not send another. (The identical email re-fetched is
    # already blocked by the ProcessedEmail claim above; this covers a reply within the thread.)
    parent_refs = [r for r in [message.get("in_reply_to"), *(message.get("references") or [])] if r]
    if parent_refs and ProcessedEmail.objects.filter(
            mailbox=mailbox, auto_reply_sent=True, message_id__in=parent_refs).exists():
        logger.info("COD_AUTO_REPLY_SKIPPED message_id=%s reason=already_replied_in_conversation",
                    gmid or "-")
        _mark_processed_complete(gmid, auto_reply_sent=False)
        return None, None, False

    subject, body = mails.render("COD_INFO", "en")     # English wording per spec, regardless of language
    refs = [message["message_id"]] if message.get("message_id") else []
    sent_id = _send_customer_email(to, subject, body,
                                   in_reply_to=message.get("message_id"), references=refs)
    logger.info("COD_AUTO_REPLY_SENT message_id=%s to=%s delivered=%s", gmid or "-", to or "-",
                bool(sent_id))
    # Mark Auto Resolved: fully handled with an auto-reply, NO ticket. The completed ProcessedEmail
    # (auto_reply_sent=True) is the record of the auto-resolved conversation.
    _mark_processed_complete(gmid, auto_reply_sent=True)
    logger.info("COD_AUTO_RESOLVED message_id=%s thread_id=%s (no ticket, no manual review).",
                gmid or "-", thread_id or "-")
    return None, None, True


def _detect_inquiry(message):
    """Inquiry sub-type for a NEW email (subject + body), or None."""
    from apps.ingestion import inquiry
    text = f"{message.get('subject', '')} {message.get('body_text', '')}"
    return inquiry.detect_inquiry_type(text)


def _brochure_attachment():
    """The company brochure PDF as [("company_profile.pdf", bytes, "application/pdf")], or []
    when none is configured / readable. Best-effort -- the Company Profile reply still goes out
    without it (and the failure is logged)."""
    import os
    path = getattr(_dj_settings(), "COMPANY_BROCHURE_PATH", "") or ""
    fname = getattr(_dj_settings(), "COMPANY_BROCHURE_FILENAME", "") or "company_profile.pdf"
    if path and os.path.isfile(path):
        try:
            with open(path, "rb") as fh:
                return [(fname, fh.read(), "application/pdf")]
        except Exception:  # noqa: BLE001
            logger.warning("INQUIRY brochure unreadable at %s -> sending without attachment.", path)
    else:
        logger.warning("INQUIRY company brochure missing (COMPANY_BROCHURE_PATH=%r) -> reply "
                       "sent WITHOUT the PDF.", path)
    return []


def _dj_settings():
    from django.conf import settings
    return settings


def _inquiry_send(pending, body, *, subject=None, attachments=None):
    """Send an inquiry message to the customer and thread it onto the conversation."""
    subj = subject or _auto_reply_subject(pending)   # explicit inquiry subject wins; else concern-based
    refs = list(pending.references or [])
    if pending.original_message_id and pending.original_message_id not in refs:
        refs.append(pending.original_message_id)
    sent_id = _send_customer_email(pending.customer_email, subj, body,
                                   in_reply_to=pending.original_message_id, references=refs,
                                   attachments=attachments)
    if sent_id:
        pending.last_message_id = sent_id
    return sent_id


def _create_inquiry_record(pending, mailbox, *, status):
    from apps.ingestion import inquiry
    from apps.tickets.models import Inquiry
    ex = pending.extracted or {}
    data = ex.get("inquiry_data") or {}
    itype = ex.get("inquiry_type")
    spec = inquiry.FLOWS.get(itype, {})
    name = (data.get("dropshipping_name") or data.get("invoice_name")
            or ex.get("sender_name") or "")
    phone = (data.get("franchise_mobile") or data.get("dropshipping_mobile")
             or data.get("invoice_mobile") or "")
    inq = Inquiry.objects.create(
        organization=pending.organization, brand=pending.brand, mailbox=mailbox,
        pending=pending, inquiry_type=itype, channel=ex.get("channel", "email"),
        status=status, queue=spec.get("queue", "") or "",
        customer_email=pending.customer_email, customer_name=name, phone=phone, data=data)
    logger.info("INQUIRY-RECORD-CREATED type=%s id=%s status=%s queue=%s data_keys=%s",
                itype, inq.id, status, inq.queue or "-", list(data.keys()))
    if inq.queue:
        logger.info("INQUIRY-QUEUED type=%s id=%s -> %s queue.", itype, inq.id, inq.queue)
    return inq


def _close_inquiry(pending):
    pending.status = "closed"
    pending.closed_at = timezone.now()


def _message_has_image(message):
    """True if the email carries an IMAGE attachment (a screenshot)."""
    for blob in message.get("attachment_blobs") or []:
        if (blob.get("mime_type") or "").lower().startswith("image/"):
            return True
    for att in message.get("attachments") or []:
        if (att.get("mime_type") or "").lower().startswith("image/"):
            return True
    return False


def _first_image_filename(message):
    for blob in message.get("attachment_blobs") or []:
        if (blob.get("mime_type") or "").lower().startswith("image/"):
            return blob.get("filename") or "screenshot.png"
    return "screenshot.png"


def _handle_inquiry_first_email(mailbox, message, detected):
    """First inbound inquiry email -> start the dedicated flow (or a MENU). NO ticket, NO
    order verification, NO M1. `detected` is a direct inquiry type OR a menu category."""
    from apps.ingestion import inquiry
    brand = mailbox.brand
    gmid = message.get("gmail_message_id") or message.get("message_id") or ""
    pending = PendingConversation.objects.create(
        organization=brand.organization, brand=brand, mailbox=mailbox,
        customer_email=message.get("from_email", ""), subject=message.get("subject", ""),
        original_message_id=gmid, last_message_id=gmid,
        thread_id=message.get("thread_id", ""), in_reply_to=message.get("in_reply_to", ""),
        references=message.get("references", []), headers=message.get("headers", {}),
        body_text=message.get("body_text", ""), body_html=message.get("body_html", ""),
        requires_evidence=False, status="inquiry_open",
        extracted={"intent": "INQUIRY", "inquiry_category": "", "inquiry_type": "",
                   "inquiry_stage": "", "inquiry_step": 0, "inquiry_data": {},
                   "channel": "email", "sender_email": message.get("from_email", ""),
                   "sender_name": (message.get("from_name") or "").strip()})
    if inquiry.is_menu_category(detected):
        ex = pending.extracted
        ex["inquiry_category"] = detected
        # Sub-category already clear in the FIRST email -> SKIP the option menu and start the
        # specific sub-flow directly ("I paid a fraud person" -> FRAUD_PAYMENT).
        subtype = inquiry.detect_menu_subcategory(
            detected, f"{message.get('subject','')} {message.get('body_text','')}")
        # Report Fraud NEVER shows an option menu -- the issue is already classified. If the
        # sub-type isn't obvious from the first email, default to Payment Fraud so a single
        # info-request email goes out immediately (never "choose an option"). Other menu
        # categories (bulk purchase) keep their menu.
        if not subtype and detected == inquiry.REPORT_FRAUD:
            subtype = inquiry.FRAUD_PAYMENT
            logger.info("INQUIRY-FRAUD sub-category unclear -> default FRAUD_PAYMENT (no menu).")
        if subtype:
            logger.info("INQUIRY-SUBCATEGORY-DETECTED category=%s -> %s (skip menu).",
                        detected, subtype)
            pending.extracted = ex
            result = _begin_inquiry_subflow(pending, mailbox, message, subtype)
            pending.save()
            return result
        ex["inquiry_stage"] = "awaiting_menu"
        pending.extracted = ex
        logger.info("INQUIRY-MENU category=%s from=%s -> showing menu (sub-category unknown).",
                    detected, message.get("from_email"))
        _inquiry_send(pending, inquiry.menu_prompt(detected))
        pending.save()
        return None, None, True

    logger.info("INQUIRY-DETECTED type=%s from=%s (dedicated inquiry flow -- NO support "
                "verification).", detected, message.get("from_email"))
    result = _begin_inquiry_subflow(pending, mailbox, message, detected)
    pending.save()
    return result


def _begin_inquiry_subflow(pending, mailbox, message, subtype):
    """Set the chosen sub-flow on the pending. SINGLE-REPLY mode: auto-reply flows (Company
    Profile brochure / VIP link / Other) complete immediately; field flows send ONE message
    listing all required details and wait for a single reply."""
    from apps.ingestion import inquiry
    from apps.tickets.models import Inquiry
    ex = dict(pending.extracted or {})
    ex["inquiry_type"] = subtype
    spec = inquiry.FLOWS.get(subtype) or {}
    if spec.get("auto_reply"):                 # Company Profile / VIP / Other -> reply now, done
        ex["inquiry_stage"] = "done"
        pending.extracted = ex
        attachments = _brochure_attachment() if spec.get("brochure") else None
        _inquiry_send(pending, spec["final"], subject=spec.get("subject"),
                      attachments=attachments)
        if spec.get("log_event"):
            logger.info("%s pending=%s to=%s (auto-reply, no ticket).",
                        spec["log_event"], pending.id, pending.customer_email)
        _create_inquiry_record(pending, mailbox, status=Inquiry.STATUS_COMPLETED)
        _close_inquiry(pending)
        return None, None, True
    # Field-collecting flow (incl. fraud) -> send ONE email listing all required details for the
    # DETECTED issue and wait for a single reply. Fraud no longer runs a separate verify-first
    # step or an option menu: the info-request goes out immediately, and the customer is verified
    # at completion from the registered mobile/email they provide (see _fraud_resolve_customer).
    ex["inquiry_stage"] = "awaiting_details"
    pending.extracted = ex
    _inquiry_send(pending, spec["intro"], subject=spec.get("subject"))
    return None, None, True


def _handle_inquiry_reply(mailbox, message, pending):
    """A reply within a running inquiry conversation -> single-reply state machine."""
    from apps.ingestion import inquiry
    ex = dict(pending.extracted or {})
    stage = ex.get("inquiry_stage")
    body = _clean_reply(message.get("body_text", "") or "").strip()
    logger.info("INQUIRY-REPLY pending=%s stage=%s type=%s body=%r", pending.id, stage,
                ex.get("inquiry_type"), body[:160])
    if ex.get("inquiry_type") in ("FRAUD_PAYMENT", "FRAUD_ALERT"):
        logger.info("FRAUD_PENDING_MATCHED pending=%s type=%s stage=%s -- reply processed by the "
                    "Fraud workflow (High-Priority engine bypassed).", pending.id,
                    ex.get("inquiry_type"), stage)

    # Menu selection -> resolve to a sub-flow and start it.
    if stage == "awaiting_menu":
        category = ex.get("inquiry_category")
        subtype = inquiry.resolve_menu_choice(category, body)
        if not subtype:
            logger.info("INQUIRY-MENU unrecognized choice -> re-showing menu.")
            _inquiry_send(pending, inquiry.menu_prompt(category))
            pending.save()
            return None, None, True
        logger.info("INQUIRY-MENU-CHOICE category=%s -> %s", category, subtype)
        result = _begin_inquiry_subflow(pending, mailbox, message, subtype)
        pending.save()
        return result

    # Fraud verification reply (STEP 1) -> verify the customer's identifier, then collect.
    if stage == "awaiting_fraud_verification":
        o, p, e = _fraud_identifier(pending, message, mailbox)
        if not (o or p or e):
            _inquiry_send(pending, _FRAUD_VERIFY_MSG)
            pending.save()
            return None, None, True
        result = _fraud_verify(pending, mailbox, message, o, p, e)
        pending.save()
        return result

    itype = ex.get("inquiry_type")
    spec = inquiry.FLOWS.get(itype) or {}

    if stage == "awaiting_details":
        # PARSE all fields from the single reply, merge with anything already captured.
        data = dict(ex.get("inquiry_data") or {})
        parsed = inquiry.parse_fields(body, inquiry.flow_all_fields(itype))
        data.update(parsed)
        logger.info("INQUIRY-PARSED type=%s parsed=%s", itype, list(parsed.keys()))
        # Fraud screenshot (mandatory before a ticket can be created).
        if inquiry.requires_attachment(itype) and _message_has_image(message):
            data[spec["attachment_field"]] = _first_image_filename(message)
            ex["_has_screenshot"] = True
            logger.info("INQUIRY-SCREENSHOT-RECEIVED %s", data[spec["attachment_field"]])
        ex["inquiry_data"] = data
        pending.extracted = ex

        missing = inquiry.missing_fields(itype, data)
        need_shot = inquiry.requires_attachment(itype) and not ex.get("_has_screenshot")
        if itype in ("FRAUD_PAYMENT", "FRAUD_ALERT"):
            logger.info("PARSED_DESCRIPTION=%r",
                        data.get("fraud_description") or data.get("call_description") or "")
            logger.info("PARSED_FRAUDSTER_MOBILE=%r",
                        data.get("fraud_mobile") or data.get("suspicious_mobile") or "")
            logger.info("SCREENSHOT_FOUND=%s", bool(ex.get("_has_screenshot")))
            logger.info("MISSING_FIELDS=%s",
                        (missing + (["screenshot"] if need_shot else [])) or "none")
        if missing or need_shot:
            _send_inquiry_missing(pending, spec, missing, need_shot)
            pending.save()
            return None, None, True
        # All required fields present -> create immediately (no follow-up questions).
        return _complete_inquiry(pending, mailbox, message, spec, ex)

    # stage == 'done' or unknown -> gentle acknowledgement, no re-collection.
    _inquiry_send(pending, "Thank you for contacting DeoDap. Our team will get back to you "
                           "shortly.")
    pending.save(update_fields=["last_message_id", "updated_at"])
    return None, None, True


def _send_inquiry_missing(pending, spec, missing, need_shot):
    """Re-ask only for the still-missing details (never a full step-by-step restart)."""
    labels = [k.split("_", 1)[-1].replace("_", " ").title() for k in missing]
    if need_shot:
        labels.append("Screenshot (attach an image)")
    logger.info("INQUIRY-INCOMPLETE pending=%s missing=%s need_screenshot=%s", pending.id,
                missing, need_shot)
    body = ("We still need the following to proceed:\n\n"
            + "\n".join(f"• {x}" for x in labels)
            + "\n\nPlease reply with these details in a single message.")
    _inquiry_send(pending, body)


def _complete_inquiry(pending, mailbox, message, spec, ex):
    """All details received -> fraud creates a Care Panel ticket (with duplicate check);
    every other flow sends the final message + creates the Inquiry record."""
    from apps.tickets.models import Inquiry
    ex["inquiry_stage"] = "done"
    pending.extracted = ex
    if spec.get("creates_ticket"):
        return _complete_fraud_inquiry(mailbox, message, pending, spec)
    _inquiry_send(pending, spec["final"])
    _create_inquiry_record(pending, mailbox, status=Inquiry.STATUS_COMPLETED)
    _close_inquiry(pending)
    pending.save()
    return None, None, True


# --- Fraud: dedicated workflow that DOES create a Care Panel ticket (HIGH priority) -------
def _open_fraud_ticket(brand, *, issue_type, fraudster_mobile):
    """An OPEN fraud ticket for the SAME INCIDENT -- the same sub-category AND the same fraudster
    number. A different fraudster number (or sub-category) is a DIFFERENT incident -> a NEW
    ticket, so a customer can report multiple distinct frauds and a fresh report is never merged
    into an unrelated old ticket. Only an exact re-report of the same fraudster dedups."""
    if not fraudster_mobile:
        return None                                  # no incident key -> always a new ticket
    from apps.classifier.rule_classifier import normalize_phone

    fm = normalize_phone(fraudster_mobile) or fraudster_mobile
    return (Ticket.objects.filter(brand=brand, extracted__fraud_report=True,
                                  extracted__fraud_issue_type=issue_type)
            .exclude(status__in=Ticket.TERMINAL_STATUSES)
            .filter(Q(extracted__fraudster_mobile=fraudster_mobile)
                    | Q(extracted__fraudster_mobile=fm))
            .order_by("-created_at").first())


def _send_existing_fraud_ticket(pending, ticket):
    number = ticket.ticket_number or ticket.ticket_id
    url = customer_ticket_link(ticket) or _care_panel_tracking_url(ticket) or (ticket.tracking_url or "")
    issue = ticket.sub_topic or ticket.issue_summary or "Report Fraud"
    created = ticket.created_at.strftime("%d %b %Y")
    lines = ["You already have open ticket(s) for this issue.", "",
             f"Ticket:\n#{number}", "", f"Issue:\n{issue}", "",
             f"Status:\n{ticket.get_status_display()}", "", f"Created:\n{created}"]
    if url:
        lines += ["", f"Track:\n{url}"]
    _inquiry_send(pending, "\n".join(lines))
    logger.info("FRAUD-DUPLICATE existing ticket=%s -> NO new ticket created.", ticket.ticket_id)


# STEP 1 wording when the customer is not (yet) verified.
_FRAUD_VERIFY_MSG = ("We could not verify the provided information.\n\n"
                     "Please reply with a valid:\n"
                     "• Order Number\n"
                     "• Mobile Number\n"
                     "• Registered Email ID")


def _fraud_identifier(pending, message, mailbox):
    """The CUSTOMER's own identifier (order / mobile / email) from the verification message AND
    the FIRST email held on the pending. NEVER the fraudster's number (collected later)."""
    from apps.classifier.rule_classifier import _extract_order_id, _extract_phone

    o, p, e = _tracking_identifiers(
        message, exclude_emails=[mailbox.email_address, pending.customer_email])
    body = pending.body_text or ""
    o = o or pending.order_id or _extract_order_id(body) or ""
    p = p or pending.phone or _extract_phone(body) or ""
    return o, p, e


def _fraud_start(pending, mailbox, message):
    """STEP 1 -- AUTO-VERIFY. Verify the customer from the first email's identifier; on success
    ask for the details (STEP 2); otherwise ask for a valid identifier (NEVER collect / create
    a ticket for an unverified customer)."""
    o, p, e = _fraud_identifier(pending, message, mailbox)
    if not (o or p or e):
        ex = dict(pending.extracted or {})
        ex["inquiry_stage"] = "awaiting_fraud_verification"
        pending.extracted = ex
        logger.info("CUSTOMER-LOOKUP-METHOD=none\nCUSTOMER-NAME=Unknown (awaiting identifier)")
        _inquiry_send(pending, _FRAUD_VERIFY_MSG)
        return None, None, True
    return _fraud_verify(pending, mailbox, message, o, p, e)


def _fraud_verify(pending, mailbox, message, o, p, e):
    """Run the Shopify check. proceed (verified OR Shopify down) -> STEP 2 details; a real
    NO-MATCH -> re-ask (STOP)."""
    from apps.ingestion import inquiry

    proceed, status, info = _verify_against_shopify(mailbox.brand, o, p, e)
    name = (info.get("customer_name") or "").strip()
    method = ("mobile" if (proceed and p) else "order" if (proceed and o)
              else "email" if (proceed and e) else "none")
    logger.info("CUSTOMER-LOOKUP-METHOD=%s", method)
    logger.info("CUSTOMER-MOBILE=%s", p or "-")
    logger.info("CUSTOMER-NAME=%s", name or "Unknown")
    ex = dict(pending.extracted or {})
    if proceed:
        # Persist the VERIFIED customer so the ticket name never depends on the sender / typed
        # name / fraudster number, and survives across the multi-reply flow.
        ex["fraud_verified"] = True
        ex["fraud_verified_info"] = info
        ex["verified_customer_name"] = name
        ex["verified_customer_mobile"] = (info.get("customer_phone") or p or "").strip()
        ex["verified_customer_email"] = (info.get("customer_email") or e or "").strip()
        ex["verified_customer_order"] = (info.get("order_id") or o or "").strip()
        ex["inquiry_stage"] = "awaiting_details"
        pending.extracted = ex
        logger.info("VERIFIED-CUSTOMER=%s", name or "(verified, no name on order)")
        _inquiry_send(pending, inquiry.FLOWS[ex["inquiry_type"]]["intro"])   # STEP 2
        return None, None, True
    ex["inquiry_stage"] = "awaiting_fraud_verification"                      # not_found -> STOP
    pending.extracted = ex
    _inquiry_send(pending, _FRAUD_VERIFY_MSG)
    return None, None, True


def _fraud_confirmation(ticket):
    """STEP 4 confirmation -- ALWAYS includes the tracking link."""
    number = ticket.ticket_number or ticket.ticket_id
    url = customer_ticket_link(ticket) or _care_panel_tracking_url(ticket) or (ticket.tracking_url or "")
    lines = ["Your complaint is registered.", "", f"Ticket ID: {number}"]
    if url:
        lines += ["", "Track Ticket:", url]
    lines += ["", "Our team will review your complaint and contact you shortly.", "",
              "Regards,", "DeoDap Support Team"]
    return "\n".join(lines)


def _fraud_resolve_customer(brand, ex, data, extra_ids=None):
    """Resolve the VERIFIED customer (Shopify info) for the fraud ticket. Verifies using the
    CUSTOMER's mobile / order / email -- from the collected 'registered' fields, the customer's
    ORIGINAL email + sender address (`extra_ids`), or a prior STEP-1 result -- NEVER the
    fraudster's number, or the typed reporter name. Empty -> 'Unknown'."""
    info = ex.get("fraud_verified_info") or {}
    if (info.get("customer_name") or "").strip():
        return info
    fraudster = {str(data.get("fraud_mobile") or ""), str(data.get("suspicious_mobile") or ""),
                 str(data.get("caller_mobile") or "")}
    mobiles, orders, emails = [], [], []
    for v in (ex.get("verified_customer_mobile"), data.get("registered_mobile")):
        v = (v or "").strip()
        if v and v not in fraudster and v not in mobiles:
            mobiles.append(v)
    o0 = (ex.get("verified_customer_order") or "").strip()
    if o0:
        orders.append(o0)
    e0 = (ex.get("verified_customer_email") or data.get("registered_email") or "").strip()
    if e0:
        emails.append(e0)
    # Identifiers taken from the customer's ORIGINAL email + sender address (no separate verify
    # step now) -- so a verified customer name still resolves. Never the fraudster's number.
    for (o, p, e) in (extra_ids or []):
        p = (p or "").strip()
        if p and p not in fraudster and p not in mobiles:
            mobiles.append(p)
        o, e = (o or "").strip(), (e or "").strip()
        if o and o not in orders:
            orders.append(o)
        if e and e not in emails:
            emails.append(e)
    attempts = [("mobile", m, ("", m, "")) for m in mobiles]
    attempts += [("order", o, (o, "", "")) for o in orders]
    attempts += [("email", e, ("", "", e)) for e in emails]
    for method, ident, (o, p, e) in attempts:
        status, found = _shopify_verify(brand, o, p, e, workflow="fraud")
        if status == "verified" and (found.get("customer_name") or "").strip():
            logger.info("CUSTOMER-LOOKUP-METHOD=%s | CUSTOMER-MOBILE=%s", method, p or "-")
            return found
    return info or {}


def _complete_fraud_inquiry(mailbox, message, pending, spec):
    brand = mailbox.brand
    ex = dict(pending.extracted or {})
    itype = ex.get("inquiry_type")
    data = ex.get("inquiry_data") or {}
    sender_email = pending.customer_email or ex.get("sender_email", "")
    reg_email = data.get("registered_email") or ""
    fraudster_phone = data.get(spec.get("phone_field", "")) or ""

    # CUSTOMER NAME = the VERIFIED order owner. Identify them from their ORIGINAL email (order id /
    # their own mobile) + sender address -- never the sender's typed name or the fraudster number.
    from apps.classifier.rule_classifier import _extract_order_id, _extract_phone
    first = pending.body_text or ""
    cust_ids = [((pending.order_id or _extract_order_id(first) or ""),
                 (_extract_phone(first) or ""), (sender_email or ""))]
    info = _fraud_resolve_customer(brand, ex, data, extra_ids=cust_ids)
    cust_phone = (info.get("customer_phone") or ex.get("verified_customer_mobile") or "").strip()
    verified_name = (info.get("customer_name") or "").strip()
    logger.info("VERIFIED-CUSTOMER=%s", verified_name or "none")
    logger.info("CUSTOMER-NAME=%s", verified_name or "Unknown")

    # DEDUP per INCIDENT (sub-category + fraudster number). Different fraudster / sub-category =>
    # a NEW ticket; only an exact re-report of the same fraudster is merged.
    dup = _open_fraud_ticket(brand, issue_type=spec["issue_type"],
                             fraudster_mobile=fraudster_phone)
    if dup is not None:
        _send_existing_fraud_ticket(pending, dup)
        _close_inquiry(pending)
        pending.save()
        return None, None, True

    issue_type = spec["issue_type"]
    label = "Payment Done to Fraudster" if itype == "FRAUD_PAYMENT" else "Suspicious Call"
    summary = (data.get("fraud_description") or data.get("call_description")
               or data.get("message_details") or f"Report Fraud - {label}")
    # Build extracted WITHOUT a sender/typed customer_name; the verified owner is stamped below.
    # The reporter's typed name is kept separately for the agent (reporter_name).
    extracted = {**data, "reporter_name": data.get("reporter_name") or data.get("customer_name")
                 or "", "fraud_report": True, "fraud_issue_type": issue_type,
                 "registered_email": reg_email, "intent": "INQUIRY",
                 "sender_email": sender_email, "sender_name": ex.get("sender_name", ""),
                 "fraudster_mobile": fraudster_phone,
                 # Care Panel phone = the CUSTOMER's verified mobile (never the fraudster's).
                 "phone": cust_phone or ""}
    extracted.pop("customer_name", None)
    extracted = _stamp_verified_customer(extracted, info)   # owner name (shopify_verified) or none
    ticket = Ticket.objects.create(
        organization=brand.organization, brand=brand, mailbox=mailbox,
        thread_id=pending.thread_id or pending.original_message_id,
        customer_email=sender_email, subject=f"Report Fraud - {label}",
        status=Ticket.STATUS_AWAITING_AGENT, priority=Ticket.PRIORITY_HIGH,
        classification_status=Ticket.CLS_CLASSIFIED,
        category="16. Feedback, Support & Fraud", sub_topic="Report Fraud",
        issue_summary=summary, extracted=extracted)
    AuditLogEntry.objects.create(ticket=ticket, actor="system", event="ticket_created",
                                 detail={"fraud": True, "issue_type": issue_type,
                                         "priority": "high", "from_pending": pending.id})
    logger.info("FRAUD-TICKET-CREATED ticket=%s issue=%s priority=HIGH from_pending=%s",
                ticket.ticket_id, issue_type, pending.id)
    logger.info("FRAUD_TICKET_CREATED ticket=%s issue=%s priority=HIGH from_pending=%s (NOT an "
                "escalation).", ticket.ticket_id, issue_type, pending.id)

    msg = Message.objects.create(
        ticket=ticket, direction=Message.DIRECTION_INBOUND,
        gmail_message_id=message.get("gmail_message_id") or pending.original_message_id,
        from_email=sender_email, subject=ticket.subject,
        body_text=pending.body_text or summary, headers=message.get("headers", {}))
    blobs = message.get("attachment_blobs") or []
    if blobs:
        _store_attachments(ticket, msg, blobs)

    _store_care_panel(ticket)            # STEP 3: Care Panel ticket + tracking hash + URL
    _ensure_tracking(ticket)
    _upload_care_panel_media(ticket)
    ticket.refresh_from_db()

    _inquiry_send(pending, _fraud_confirmation(ticket))   # STEP 4 (always includes the link)
    logger.info("FRAUD_CONFIRMATION_SENT ticket=%s to=%s (Care Panel link included).",
                ticket.ticket_id, pending.customer_email)
    _close_inquiry(pending)
    pending.save()
    logger.info("FRAUD_PENDING_COMPLETED pending=%s -> ticket=%s (fraud workflow closed the "
                "conversation; no escalation).", pending.id, ticket.ticket_id)
    return ticket, ticket.messages.order_by("created_at").last(), True


def _send_verification_request(pending):
    """STEP 1: acknowledge the inquiry and ask for an identifier (M_VERIFY_REQUEST). NO
    ticket, NO Shopify call. Keeps the pending OPEN."""


def _send_verification_request(pending):
    """STEP 1: acknowledge the inquiry and ask for an identifier (M_VERIFY_REQUEST). NO
    ticket, NO Shopify call. Keeps the pending OPEN."""
    # STEP 1 spec: the acknowledgement subject is exactly "Request Received" (not "Re: ...").
    # The reply still threads to this pending via In-Reply-To (last_message_id) / References.
    subject, body = mails.render("M_VERIFY_REQUEST", pending.language)
    refs = list(pending.references or [])
    if pending.original_message_id and pending.original_message_id not in refs:
        refs.append(pending.original_message_id)
    sent_id = _send_customer_email(pending.customer_email, subject, body,
                                   in_reply_to=pending.original_message_id, references=refs)
    pending.evidence_requests = (pending.evidence_requests or 0) + 1
    if sent_id:
        pending.last_message_id = sent_id
    pending.save(update_fields=["evidence_requests", "last_message_id", "updated_at"])
    logger.info("VERIFY-REQUEST sent to %s (pending=%s, kind=%s) -- no ticket.",
                pending.customer_email, pending.id, (pending.extracted or {}).get("verify_kind"))


def _send_verification_failed(pending):
    """STEP 4: the provided details could not be verified -> ask again (M_VERIFY_FAILED).
    NO ticket, pending stays OPEN."""
    logger.info("VERIFICATION-FAILED pending=%s customer=%s -> M_VERIFY_FAILED (stays held, "
                "no ticket).", pending.id, pending.customer_email)
    subject, body = mails.render("M_VERIFY_FAILED", pending.language)
    subj = _auto_reply_subject(pending)
    refs = list(pending.references or [])
    if pending.original_message_id and pending.original_message_id not in refs:
        refs.append(pending.original_message_id)
    from django.conf import settings as _dj
    _from = getattr(_dj, "REPLY_FROM", "") or getattr(_dj, "IMAP_USER", "") or "-"
    logger.info("AUTO-REPLY-GENERATED pending=%s template=M_VERIFY_FAILED from=%s to=%s "
                "subject=%r original_message_id=%s in_reply_to=%s references=%s",
                pending.id, _from, pending.customer_email, subj,
                pending.original_message_id or "-", pending.original_message_id or "-",
                refs or "-")
    sent_id = _send_customer_email(pending.customer_email, subj, body,
                                   in_reply_to=pending.original_message_id, references=refs)
    pending.evidence_requests = (pending.evidence_requests or 0) + 1
    if sent_id:
        pending.last_message_id = sent_id
    pending.save(update_fields=["evidence_requests", "last_message_id", "updated_at"])
    logger.info("AUTO-REPLY-SENT M_VERIFY_FAILED to=%s pending=%s sent_id=%s.",
                pending.customer_email, pending.id, sent_id or "-")


# After this many failed verification replies we stop looping and create the ticket
# anyway (with the identifier the customer DID provide) so they are never trapped --
# a human then sorts out an order number we couldn't match. See _handle_verification_pending.
MAX_VERIFY_ATTEMPTS = 2


def _shopify_verify(brand, order_id, phone, email, *, workflow="verify"):
    """THE single Shopify identity check shared by EVERY ticket-creating workflow (tracking,
    evidence, invoice). The same order / mobile / email runs through the SAME lookup_tracking
    call here, so a number that verifies for one workflow verifies IDENTICALLY for all others
    -- there is no second, divergent lookup or phone-normalization path.

    Returns (status, info) where status is one of:
      'no_identifier' -- nothing to look up -> ask the customer for an identifier.
      'verified'      -- Shopify returned a matching order.
      'not_found'     -- Shopify was reachable but returned NO match.
      'cannot_verify' -- Shopify not configured / errored (don't trap the customer).
    """
    # IDENTIFIER-EXTRACTED: every identifier we will try (OR logic -- any ONE may verify).
    logger.info("IDENTIFIER-EXTRACTED workflow=%s order_id=%s mobile=%s email=%s",
                workflow, order_id or "-", phone or "-", email or "-")
    if not (order_id or phone or email):
        logger.info("VERIFICATION-FAILED workflow=%s reason=no_identifier", workflow)
        return "no_identifier", {}

    from apps.integrations import context as live_context

    # lookup_tracking tries order -> mobile -> email and stops at the FIRST that matches.
    info = live_context.lookup_tracking(brand, order_id=order_id, phone=phone, email=email)
    if info.get("error") or not info.get("configured"):
        logger.info("VERIFICATION-FAILED workflow=%s reason=cannot_verify configured=%s "
                    "error=%s (accepted -- not trapping the customer)", workflow,
                    info.get("configured"), info.get("error"))
        return "cannot_verify", info          # can't run the check -> don't loop the customer
    if info.get("found"):
        logger.info("VERIFICATION-SUCCESS workflow=%s verified_by=%s identifier=%s "
                    "resolved_order=%s", workflow, info.get("matched_by"),
                    info.get("matched_identifier"), info.get("order_id"))
        return "verified", info
    logger.info("VERIFICATION-FAILED workflow=%s reason=no_match order=%s mobile=%s email=%s "
                "(all three failed)", workflow, order_id or "-", phone or "-", email or "-")
    return "not_found", info


def _stamp_verified_customer(extracted, info):
    """ORDER OWNER ALWAYS WINS: when a valid Shopify order is found, the ticket's customer
    identity (name / phone / email) becomes the ORDER OWNER's -- never the email sender's.
    The sender stays available separately (ticket.customer_email + the inbound message) for
    conversation history and reply routing only."""
    info = info or {}
    name = (info.get("customer_name") or "").strip()
    if name:
        extracted["customer_name"] = name
        extracted["customer_name_source"] = "shopify_verified"
        logger.info("CUSTOMER-NAME-SOURCE shopify_verified -> TICKET-CUSTOMER-NAME %s", name)
    if info.get("order_id") and not extracted.get("order_id"):
        extracted["order_id"] = info["order_id"]   # the order phone/email resolved to
    # ORDER OWNER phone -- ALWAYS the order's phone (overrides any sender-typed number). The
    # Care Panel store-json API is phone-keyed: without it no tracking link is created.
    phone = (info.get("customer_phone") or "").strip()
    if phone:
        extracted["phone"] = phone
        logger.info("VERIFIED-CUSTOMER-PHONE from Shopify order -> %s", phone)
    # ORDER OWNER email -- shown on the ticket / Care Panel (NOT the sender's email, which is
    # kept on ticket.customer_email for reply routing).
    email = (info.get("customer_email") or "").strip()
    if email:
        extracted["customer_email"] = email
        logger.info("VERIFIED-CUSTOMER-EMAIL from Shopify order -> %s", email)
    return extracted


def _capture_sender_identity(extracted, message):
    """Record the actual email SENDER (sender_name / sender_email) separately. The sender is
    used ONLY for conversation history and reply routing -- NEVER as the ticket customer
    identity (which is the Shopify order owner when an order is found)."""
    sender_email = (message.get("from_email") or "").strip()
    sender_name = (message.get("from_name") or "").strip().strip('"')
    if sender_email:
        extracted["sender_email"] = sender_email
    if sender_name:
        extracted["sender_name"] = sender_name
    return extracted


def _verify_inquiry_identifier(brand, kind, order_id, phone, email):
    """STEP 2: classify the reply's identifier for the inquiry kind. Returns (status, ref, info).
    invoice requires a live Shopify order match (via the SHARED _shopify_verify); franchise /
    dropship / company accept a valid mobile OR email."""
    if not (order_id or phone or email):
        return "no_identifier", "", {}
    if kind != "invoice":
        return "verified", (phone or email or order_id), {}
    status, info = _shopify_verify(brand, order_id, phone, email, workflow="invoice")
    return status, (info.get("order_id") or order_id or phone or email), info


def _verify_against_shopify(brand, order_id, phone, email):
    """Evidence-category verification gate -- uses the SAME _shopify_verify as tracking and
    invoice. Returns (proceed, status, info). `proceed` is True on a real order MATCH and also
    when the check is impossible (Shopify down / not configured) -- we never trap a customer
    behind a broken integration. False only on an explicit NO-MATCH / no identifier."""
    status, info = _shopify_verify(brand, order_id, phone, email, workflow="evidence")
    return status in ("verified", "cannot_verify"), status, info


def _verify_cancellation_identifier(brand, *, order_id="", phone="", email="", awb=""):
    """Verify a cancellation identifier BEFORE any ticket is created (fixes: a bare number was
    stored as order_id and a ticket created without checking Shopify). Reuses the SHARED Shopify
    verifier for order / phone / email; a value that is not a Shopify order is then checked as a
    courier AWB (shipping.verify_awb, which reuses shipping.track). Returns
    (proceed, status, info, verified_awb). proceed is True on a real match OR when Shopify is down
    (cannot_verify -> never trap the customer); False only on a genuine not_found / no identifier."""
    logger.info("ORDER_VERIFICATION_STARTED order=%s phone=%s email=%s awb=%s",
                order_id or "-", phone or "-", email or "-", awb or "-")
    proceed, status, info = _verify_against_shopify(brand, order_id, phone, email)
    verified_awb = ""
    if not proceed and awb:
        from apps.integrations import shipping as _shipping
        if _shipping.verify_awb(brand, awb):
            proceed, status, verified_awb = True, "verified", awb
    if proceed:
        logger.info("ORDER_VERIFIED status=%s order=%s awb=%s customer=%s", status,
                    (info or {}).get("order_id") or order_id or "-", verified_awb or awb or "-",
                    (info or {}).get("customer_name") or "-")
    else:
        logger.info("ORDER_NOT_FOUND status=%s order=%s phone=%s email=%s awb=%s", status,
                    order_id or "-", phone or "-", email or "-", awb or "-")
    return proceed, status, info, verified_awb


# --------------------------------------------------------------------------------------- #
# Double Payment / Payment Deducted Twice -- PROGRESSIVE collection (NEVER an immediate ticket).
# Collect BOTH a Registered Mobile Number (parsed from the reply text) AND a Payment Screenshot
# (photo attachment) before creating a ticket. Ask ONLY for what is still missing, never repeat
# the same request, verify the customer by mobile, then promote (ticket + M5 confirmation). The
# pending is consumed on promotion, so a re-fetch can never create a duplicate ticket.
# --------------------------------------------------------------------------------------- #
def _is_double_payment(message, result):
    """True when this email is a 'double payment / deducted twice / paid twice' concern."""
    from apps.decision import policy

    blob = " ".join(filter(None, [
        message.get("subject", "") or "", message.get("body_text", "") or "",
        getattr(result, "sub_topic", "") or "", getattr(result, "issue_summary", "") or ""]))
    return policy.double_payment(blob)


def _dp_extract_mobile(message):
    from apps.classifier.rule_classifier import _extract_phone

    return _extract_phone(f"{message.get('subject','')} {message.get('body_text','')}") or ""


def _handle_double_payment_first_email(mailbox, message, result):
    """FIRST email of a Double Payment concern: hold a pending (NEVER a ticket yet), record
    whatever the email already carries (mobile / screenshot), then progress."""
    pending = _create_pending(mailbox, message, result, status="awaiting_evidence")
    pending.extracted = {**(pending.extracted or {}), "intent": "DOUBLE_PAYMENT"}
    fields = ["extracted"]
    mobile = _dp_extract_mobile(message)
    if mobile and not pending.phone:
        pending.phone = mobile
        fields.append("phone")
    if _message_has_photo(message) and not pending.has_photo:
        pending.has_photo = True
        fields.append("has_photo")
    pending.save(update_fields=list(dict.fromkeys(fields)) + ["updated_at"])
    logger.info("DOUBLE-PAYMENT-START pending=%s mobile=%s screenshot=%s", pending.id,
                bool(pending.phone), bool(pending.has_photo))
    return _double_payment_step(mailbox, message, pending)


def _handle_double_payment_pending(mailbox, message, pending):
    """A reply on a held Double Payment conversation: re-parse the mobile + detect a screenshot
    from THIS reply (attachments were already folded in by _accumulate_pending), then progress."""
    mobile = _dp_extract_mobile(message)
    fields = []
    if mobile and not pending.phone:
        pending.phone = mobile
        fields.append("phone")
    if _message_has_photo(message) and not pending.has_photo:
        pending.has_photo = True
        fields.append("has_photo")
    if fields:
        pending.save(update_fields=fields + ["updated_at"])
    return _double_payment_step(mailbox, message, pending)


def _double_payment_step(mailbox, message, pending):
    """Core progressive gate. Ask ONLY for the missing item(s); once BOTH the mobile AND the
    screenshot are present, verify the customer by mobile and create the ticket (+ M5 email)."""
    have_mobile = bool(pending.phone)
    have_shot = bool(pending.has_photo)
    logger.info("DOUBLE-PAYMENT-STATE pending=%s have_mobile=%s have_screenshot=%s",
                pending.id, have_mobile, have_shot)
    if have_mobile and have_shot:
        proceed, status, info = _verify_against_shopify(mailbox.brand, "", pending.phone, "")
        if proceed:
            pending.extracted = _stamp_verified_customer({**(pending.extracted or {})}, info)
            pending.save(update_fields=["extracted", "updated_at"])
            logger.info("DOUBLE-PAYMENT-VERIFIED pending=%s status=%s -> creating ticket.",
                        pending.id, status)
            ticket = _promote_pending(mailbox, pending, message)   # ticket + Care Panel + M5; clears pending
            return ticket, ticket.messages.order_by("created_at").last(), True
        # Mobile did not verify. Cap the retries so a genuine payment complaint is never trapped.
        attempts = int((pending.extracted or {}).get("dp_verify_attempts", 0)) + 1
        pending.extracted = {**(pending.extracted or {}), "dp_verify_attempts": attempts}
        if attempts >= MAX_VERIFY_ATTEMPTS:
            pending.save(update_fields=["extracted", "updated_at"])
            logger.info("DOUBLE-PAYMENT-UNVERIFIED pending=%s attempts=%s -> create ticket (agent).",
                        pending.id, attempts)
            ticket = _promote_pending(mailbox, pending, message)
            return ticket, ticket.messages.order_by("created_at").last(), True
        pending.phone = ""      # drop the unverified number so a fresh one is parsed next reply
        pending.save(update_fields=["phone", "extracted", "updated_at"])
        _send_double_payment_request(mailbox, message, pending, need_mobile=True,
                                     need_shot=False, reason="unverified_mobile")
        return None, None, True
    _send_double_payment_request(mailbox, message, pending,
                                 need_mobile=not have_mobile, need_shot=not have_shot)
    return None, None, True


def _send_double_payment_request(mailbox, message, pending, *, need_mobile, need_shot, reason=""):
    """Ask ONLY for the still-missing item(s). Never re-sends the SAME request (tracked via
    extracted['dp_asked']) and never asks for evidence already received."""
    missing = (["mobile"] if need_mobile else []) + (["screenshot"] if need_shot else [])
    if not missing:
        return
    ask_key = reason or "+".join(missing)
    if (pending.extracted or {}).get("dp_asked") == ask_key:
        logger.info("DOUBLE-PAYMENT-SKIP pending=%s ask=%s already sent -- not repeating.",
                    pending.id, ask_key)
        return
    if reason == "unverified_mobile":
        lines = ["We could not verify the mobile number you shared for the double-payment refund.",
                 "",
                 "Please reply with your Registered Mobile Number (the number used to place the order)."]
    else:
        lines = ["Thank you for reaching out about the double payment (amount deducted twice).",
                 "To verify and process your refund, please reply with:", ""]
        if need_mobile:
            lines.append("• Registered Mobile Number")
        if need_shot:
            lines.append("• Payment Screenshot")
    code = mails.normalize_lang(pending.language)
    body = "\n".join(lines) + f"\n\n{mails.SIGN[code]}"
    subject = _auto_reply_subject(pending)
    refs = list(pending.references or [])
    if pending.original_message_id and pending.original_message_id not in refs:
        refs.append(pending.original_message_id)
    sent_id = _send_customer_email(pending.customer_email, subject, body,
                                   in_reply_to=pending.original_message_id, references=refs)
    pending.extracted = {**(pending.extracted or {}), "dp_asked": ask_key}
    pending.evidence_requests = (pending.evidence_requests or 0) + 1
    pending.status = "awaiting_evidence"
    if sent_id:
        pending.last_message_id = sent_id
    pending.save(update_fields=["extracted", "evidence_requests", "status", "last_message_id",
                                "updated_at"])
    logger.info("DOUBLE-PAYMENT-REQUEST pending=%s need_mobile=%s need_screenshot=%s ask=%s",
                pending.id, need_mobile, need_shot, ask_key)


# A reply that is essentially ONLY a number (optionally +91 / spaces / dashes / #).
_ONLY_NUMBER_RE = re.compile(r"^[+()\-\s#]*\d[\d()\-\s]*$")


def _verification_identifiers(pending, message, *, exclude_emails=()):
    """Detect the identifier(s) in a verification reply -> (order_id, phone, email).

    Priority for a STANDALONE number (the reported case: a customer replies with just a number):
    the number is offered as an ORDER first and, ONLY when it is a 10-digit mobile, ALSO as a
    PHONE. Because lookup_tracking tries order -> mobile -> email in that order, a 10-digit ORDER
    number is verified as an order and never mis-read as a phone. An email is the registered email.
    A labelled / mixed reply falls back to the shape-based extractors (and the pending's stored
    values) so existing flows are unchanged."""
    from apps.classifier.rule_classifier import _extract_order_id, _extract_phone, normalize_phone

    body = _clean_reply(message.get("body_text", "") or "")
    text = f"{message.get('subject', '') or ''} {body}"
    email = _extract_email(body, exclude=exclude_emails)
    stripped = (body or "").strip()
    if not email and stripped and _ONLY_NUMBER_RE.match(stripped):
        digits = re.sub(r"\D", "", stripped)
        if digits:
            mobile = normalize_phone(digits)          # 10-digit mobile, else ""
            return digits, (mobile or ""), ""         # order first; mobile only if 10-digit
    order = _extract_order_id(text) or (pending.order_id if pending else "") or ""
    phone = _extract_phone(text) or (pending.phone if pending else "") or ""
    return order, phone, email


def _verify_reply_identifier(pending, brand, message, *, exclude_emails=()):
    """Verify a reply's identifier with ORDER -> MOBILE -> EMAIL priority (see
    _verification_identifiers). Returns (proceed, status, info, order_id, phone, email) and emits
    the IDENTIFIER_DETECTED / TRY_*_LOOKUP / SHOPIFY_MATCH|SHOPIFY_NO_MATCH log trail."""
    order_id, phone, email = _verification_identifiers(pending, message, exclude_emails=exclude_emails)
    logger.info("IDENTIFIER_DETECTED order=%s mobile=%s email=%s",
                order_id or "-", phone or "-", email or "-")
    if order_id:
        logger.info("TRY_ORDER_LOOKUP order=%s", order_id)
    if phone:
        logger.info("TRY_MOBILE_LOOKUP mobile=%s", phone)
    if email:
        logger.info("TRY_EMAIL_LOOKUP email=%s", email)
    # lookup_tracking (via _verify_against_shopify) tries order -> mobile -> email, stopping at the
    # FIRST match -> exactly the required order-before-mobile priority.
    proceed, status, info = _verify_against_shopify(brand, order_id, phone, email)
    if status == "verified":
        logger.info("SHOPIFY_MATCH matched_by=%s resolved_order=%s customer=%s",
                    info.get("matched_by") or "-", info.get("order_id") or "-",
                    info.get("customer_name") or "-")
    elif status == "not_found":
        logger.info("SHOPIFY_NO_MATCH order=%s mobile=%s email=%s",
                    order_id or "-", phone or "-", email or "-")
    return proceed, status, info, order_id, phone, email


def _clear_awaiting_verification(pending):
    ex = dict(pending.extracted or {})
    if ex.pop("awaiting_verification", None) is not None:
        pending.extracted = ex
        pending.save(update_fields=["extracted", "updated_at"])


def _handle_evidence_verification_request(mailbox, message, result, status):
    """Evidence category, FIRST email, NOT verified and NO proof attached (STEP 7): hold a
    pending that AWAITS verification (not evidence yet) and ask for an identifier. We do NOT
    ask for a photo/video until the customer is verified."""
    pending = _create_pending(mailbox, message, result)
    ex = {**(pending.extracted or {}), "awaiting_verification": True}
    pending.extracted = ex
    pending.save(update_fields=["extracted", "updated_at"])
    logger.info("VERIFICATION-FAILED pending=%s status=%s -> verify first: asking for an "
                "identifier, NOT evidence.", pending.id, status)
    _send_verification_failed(pending)   # STEP 7 wording (asks order / mobile / email)
    return None, None, True


def _request_pending_evidence(mailbox, message, pending):
    """Ask the customer for the proof the pending's category requires. Used right after a
    successful evidence-category verification. A DELIVERED-ITEM concern (Damaged / Defective /
    Non-working / Wrong Product / Wrong Parcel / Missing) uses its OWN case-specific template
    (EV_*) via _send_delivered_evidence_request -- the SAME routing the first-email and reply
    gates use; only when no delivered-item case is detected do we fall back to the generic
    video/photo request. (Previously this path ALWAYS sent the generic M2/M2P, so the new
    per-concern templates were ignored for conversations that verified first.)"""
    case = _pending_delivered_case(pending)
    if case is not None:
        # The customer may have attached evidence WITH the verifying reply (already accumulated).
        if _pending_meets_case(pending, case):
            ticket = _promote_pending(mailbox, pending, message)   # complete -> create the ticket now
            return ticket, ticket.messages.order_by("created_at").last(), True
        if pending.has_photo or pending.has_video:
            _send_progressive_evidence_request(mailbox, message, pending, case)  # ask only the rest
        else:
            _send_delivered_evidence_request(mailbox, message, pending, case)    # first full request
        return None, None, True
    level = _pending_evidence_level(pending)
    if level == evidence.EV_VIDEO and not pending.has_video:
        _send_video_request(mailbox, message, pending)
    else:
        _send_photo_request(mailbox, message, pending)
    return None, None, True


def _handle_verification_first_email(mailbox, message, result, kind):
    """Inquiry STEP 1 (universal verification rule). If the FIRST email already carries a
    valid identifier (order number / registered mobile / registered email), verify + process
    IMMEDIATELY (create the ticket, send the confirmation) -- NO M_VERIFY_REQUEST ask.
    Otherwise create a pending and ask for an identifier; the reply is verified later."""
    pending = _create_pending(mailbox, message, result)
    extracted = {**(pending.extracted or {}), "verify_kind": kind}
    pending.extracted = extracted
    pending.save(update_fields=["extracted", "updated_at"])
    order_id, phone, email = _tracking_identifiers(
        message, exclude_emails=[mailbox.email_address, pending.customer_email])
    order_id = order_id or pending.order_id or ""
    phone = phone or pending.phone or ""

    if order_id or phone or email:
        status, ref, info = _verify_inquiry_identifier(mailbox.brand, kind, order_id, phone, email)
        logger.info("IDENTIFIER-DETECTED category=%s pending=%s order=%s phone=%s email=%s "
                    "status=%s", kind, pending.id, order_id or "-", phone or "-", email or "-",
                    status)
        # Only a GENUINE match processes immediately on the first email. 'not_found' /
        # 'cannot_verify' fall through to the ask (the reply path then applies the escape
        # hatch so the customer is never trapped re-sending the same identifier).
        if status == "verified":
            ex = {**(pending.extracted or {})}
            if phone:
                ex["phone"] = phone
            if order_id:
                ex["order_id"] = order_id
            if email:
                ex["verified_email"] = email
            _stamp_verified_customer(ex, info)
            pending.extracted = ex
            pending.phone = pending.phone or phone
            pending.order_id = pending.order_id or order_id
            pending.save(update_fields=["extracted", "phone", "order_id", "updated_at"])
            logger.info("VERIFICATION-SUCCESS pending=%s kind=%s ref=%s -> "
                        "SKIP-VERIFICATION-EMAIL, creating ticket on first email.",
                        pending.id, kind, ref)
            ticket = _promote_pending(mailbox, pending, message)
            return ticket, ticket.messages.order_by("created_at").last(), True
        logger.info("VERIFICATION-FAILED pending=%s kind=%s status=%s -> sending verification "
                    "email on first email.", pending.id, kind, status)

    # No (matched) identifier -> ask (M_VERIFY_REQUEST). Don't carry stray classifier values.
    pending.phone = ""
    pending.order_id = ""
    pending.save(update_fields=["phone", "order_id", "updated_at"])
    _send_verification_request(pending)
    return None, None, True


def _handle_verification_pending(mailbox, message, pending):
    """Inquiry STEP 2-4: verify the identifier the customer replied with.
      * verified  -> promote to a real ticket (Care Panel hash + M5 View-Ticket link) and
                     close the pending (STEP 3).
      * not verified -> ask again (M_VERIFY_FAILED); the pending stays OPEN (STEP 4)."""
    kind = (pending.extracted or {}).get("verify_kind", "")
    raw_body = message.get("body_text", "") or ""
    parsed_body = _clean_reply(raw_body)
    order_id, phone, email = _tracking_identifiers(
        message, exclude_emails=[mailbox.email_address, pending.customer_email])
    order_id = order_id or pending.order_id or ""
    phone = phone or pending.phone or ""
    status, ref, info = _verify_inquiry_identifier(mailbox.brand, kind, order_id, phone, email)
    attempts = ((pending.extracted or {}).get("verify_attempts") or 0)
    logger.info(
        "VERIFY-INQUIRY pending=%s kind=%s status=%s attempts=%s order=%s phone=%s email=%s "
        "raw_body=%r parsed_body=%r",
        pending.id, kind, status, attempts, order_id or "-", phone or "-", email or "-",
        raw_body[:300], parsed_body[:300])

    # Escape hatch: a 'not_found'/'no_identifier' reply that has, by now, given us SOME
    # identifier and exhausted MAX_VERIFY_ATTEMPTS is promoted anyway (flagged unverified)
    # so the customer is never trapped re-sending the same order number (the reported bug).
    has_identifier = bool(order_id or phone or email)
    escalate = has_identifier and attempts + 1 >= MAX_VERIFY_ATTEMPTS

    if status in ("verified", "cannot_verify") or escalate:
        # Fold the identifiers into the pending (so the Care Panel store has the phone for a
        # tracking hash), then promote to a ticket and close the pending.
        extracted = {**(pending.extracted or {})}
        if phone:
            extracted["phone"] = phone
        if order_id:
            extracted["order_id"] = order_id
        if email:
            extracted["verified_email"] = email
        if status != "verified":
            extracted["verify_unconfirmed"] = status   # 'cannot_verify' / 'not_found' / ...
        _stamp_verified_customer(extracted, info)
        pending.extracted = extracted
        pending.phone = pending.phone or phone
        pending.order_id = pending.order_id or order_id
        pending.save(update_fields=["extracted", "phone", "order_id", "updated_at"])
        logger.info("VERIFICATION-SUCCESS pending=%s kind=%s status=%s escalate=%s ref=%s -> "
                    "SKIP-VERIFICATION-EMAIL, creating ticket.", pending.id, kind, status,
                    escalate, ref)
        ticket = _promote_pending(mailbox, pending, message)  # ticket + Care Panel + M5; closes pending
        return ticket, ticket.messages.order_by("created_at").last(), True

    # Still unverified and under the attempt cap -> ask again (M_VERIFY_FAILED), pending OPEN.
    logger.info("VERIFICATION-FAILED pending=%s kind=%s status=%s -> sending verification "
                "email, pending stays open.", pending.id, kind, status)
    extracted = {**(pending.extracted or {}), "verify_attempts": attempts + 1}
    pending.extracted = extracted
    pending.save(update_fields=["extracted", "updated_at"])
    _send_verification_failed(pending)
    return None, None, True


# GLOBAL VERIFICATION RULE -- the fixed categories that concern a SPECIFIC existing order.
# ANY issue in one of these MUST be verified (order / mobile / email, OR-based) before a
# ticket is created. The complement (9 Product Inquiry, 10 Offers, 11 B2B/Wholesale,
# 12 Coverage, 13 Company Info, 14 Account, 15 Website Tech, 16 Feedback/Fraud) are general
# or pre-purchase requests and may create a ticket immediately.
ORDER_RELATED_CATEGORY_CODES = {"1", "2", "3", "4", "5", "6", "7", "8"}


def _category_code(result):
    """The fixed taxonomy code (1-16) for this classification -- from the category_ref or the
    'N. Name' prefix the AI returns. '' when neither is available (Uncategorized)."""
    ref = getattr(result, "category_ref", None)
    if ref is not None and getattr(ref, "code", ""):
        return str(ref.code).strip()
    m = re.match(r"\s*#?(\d{1,2})\b", str(getattr(result, "category", "") or ""))
    return m.group(1) if m else ""


def _is_order_related(result):
    """True if the issue concerns a SPECIFIC order (category codes 1-8)."""
    code = _category_code(result)
    return bool(code) and code in ORDER_RELATED_CATEGORY_CODES


def _result_requires_ticket(result, message):
    """VERIFICATION-FIRST RULE: True when this classification will create a support ticket
    (per the authoritative policy taxonomy) -> the customer MUST be verified (order / mobile /
    email) BEFORE the ticket is created. Auto-reply categories (tracking / offers / product /
    coverage / inquiry self-serve) return False -- no ticket, so no verification needed.

    UNCATEGORIZED (no category code) is NOT gated: we can't classify it, so we don't demand
    an order number -- it is routed to a human agent by the decision engine."""
    from apps.decision import policy
    code = _category_code(result)
    if not code:
        return False
    text = " ".join([result.sub_topic or "", result.issue_summary or "",
                     message.get("subject", "") or "", message.get("body_text", "") or ""])
    return policy.requires_ticket(code, result.sub_topic or "", text)


# VERIFICATION-FIRST: categories tied to a customer's ORDER or ACCOUNT must verify before
# ANY action (ticket OR auto-reply). Pure business inquiries (franchise / dropship / company
# / bulk -- handled earlier by the inquiry workflow) and general pre-sale info (product /
# coverage / store info) are NOT tied to an order and skip verification.
VERIFICATION_REQUIRED_CATEGORY_CODES = {"1", "2", "3", "4", "5", "6", "7", "8",
                                        "10", "14", "15", "16"}


def _requires_verification(result):
    """True when the customer must be verified (order / mobile / email) BEFORE any action --
    ticket OR auto-reply. Driven by the fixed category code; codes 9 / 11 / 12 / 13 (product
    info / B2B / coverage / store info) and UNCATEGORIZED are general/pre-sale and skip it."""
    code = _category_code(result)
    return bool(code) and code in VERIFICATION_REQUIRED_CATEGORY_CODES


def _handle_order_verification_request(mailbox, message, result, status):
    """Order-related issue that did NOT verify -> hold a pending that AWAITS verification and
    ask for an identifier (order number / registered mobile / registered email). NO ticket is
    created until the customer is verified."""
    pending = _create_pending(mailbox, message, result, status="awaiting_verification")
    ex = {**(pending.extracted or {}), "awaiting_verification": True}
    pending.extracted = ex
    pending.save(update_fields=["extracted", "status", "updated_at"])
    logger.info("TICKET-BLOCKED-VERIFICATION pending=%s status=%s -> asking for order / mobile "
                "/ email; no ticket.", pending.id, status)
    _send_verification_failed(pending)
    return None, None, True


def _needs_order(result):
    """Whether to ask the customer to identify their ORDER (M1) before proceeding.

    Only for NON-evidence intents that mandate an order (e.g. order status). Evidence
    cases (damaged / defective / missing / wrong) ask for the PHOTO/VIDEO instead -- we
    never block them on 'could not locate your order'. Route A FAQ needs no order."""
    if _needs_evidence(result) or _result_requires_video(result):
        return False
    sub = result.sub_topic_ref
    return bool(sub is not None and "order_id" in (sub.mandatory_inputs or []))


def _resolve_identity_or_request(mailbox, message, result):
    """Self-lookup (§3b/§6): resolve the order from the sender's contact BEFORE asking.

    * exactly one match -> adopt its order id onto the classification (no question).
    * Shopify configured but no/ambiguous match AND the intent needs an order -> send
      M1 and hold the case. Returns the handled tuple, else None to continue the flow.
    * Shopify not configured -> None (existing behaviour untouched).
    """
    from apps.integrations import identity

    extracted = dict(result.extracted or {})
    ident = identity.resolve_identity(mailbox.brand, message, extracted=extracted)
    if not ident["configured"]:
        return None

    if ident["order"] is not None and not extracted.get("order_id"):
        oid = ident["order"].get("order_id") or ident["order"].get("name")
        if oid:
            extracted["order_id"] = oid
            extracted["identity_source"] = ident["source"]
            result.extracted = extracted
            logger.info("SELF-LOOKUP adopted order_id=%s source=%s for %s",
                        oid, ident["source"], message.get("from_email"))
        return None

    if ident["order"] is None and not extracted.get("order_id") and _needs_order(result):
        pending = _create_pending(mailbox, message, result)
        _send_identity_request(mailbox, message, pending)
        logger.info("IDENTITY-REQUEST M1 sent for %s needs_choice=%s source=%s",
                    message.get("from_email"), ident["needs_choice"], ident["source"])
        return None, None, True

    return None


def _finalize_and_confirm(ticket, kind):
    """Run the best-effort finalize steps (Care Panel sync / store / tracking / internal-link /
    media) and then ALWAYS send the customer confirmation.

    GOLDEN RULE: once the ticket EXISTS, the confirmation email MUST go out. The finalize steps
    are best-effort integrations (Care Panel API, media upload, tracking) -- a failure in any of
    them is logged but must NEVER prevent the email. send_confirmation() re-runs store+tracking
    itself (also guarded) and falls back to the no-link M5N variant, so the customer is always
    notified. This is the fix for "evidence uploaded -> ticket created -> confirmation never sent":
    an exception between create and confirm used to abort the whole flow."""
    try:
        _sync_external(ticket)
        _store_care_panel(ticket)
        _ensure_tracking(ticket)
        _upload_care_panel_media(ticket)
    except Exception:  # noqa: BLE001 -- never let a finalize step block the customer confirmation
        logger.exception("FINALIZE_PARTIAL_FAILURE ticket=%s kind=%s -- a finalize step raised; "
                         "sending the confirmation anyway.", ticket.ticket_id, kind)
    return send_confirmation(ticket, kind)


def _promote_pending(mailbox, pending, message):
    """The customer replied with evidence -> create the real Ticket now (its id is
    generated here, never before), reconstruct the conversation, attach evidence,
    classify, sync to Care Panel, and confirm."""
    from apps.classifier.service import ClassificationResult, apply_to_ticket

    brand = mailbox.brand
    logger.info("CREATE_TICKET_START pending=%s customer=%s category=%s sub_topic=%s",
                pending.id, pending.customer_email, pending.category or "-",
                pending.sub_topic or "-")
    thread_id = pending.thread_id or pending.original_message_id or message.get("thread_id", "")
    ticket = Ticket.objects.create(
        organization=brand.organization, brand=brand, mailbox=mailbox,
        thread_id=thread_id, customer_email=pending.customer_email,
        subject=pending.subject, status=Ticket.STATUS_NEW,
    )
    AuditLogEntry.objects.create(
        ticket=ticket, actor="system", event="ticket_created",
        detail={"from_pending": pending.id, "thread_id": thread_id},
    )
    logger.info("TICKET-CREATED ticket=%s from_pending=%s category=%s (verified before create)",
                ticket.ticket_id, pending.id, pending.category or "-")
    logger.info("CREATE_TICKET_SUCCESS ticket=%s from_pending=%s", ticket.ticket_id, pending.id)

    # 1) Recreate the ORIGINAL inbound email (held in the pending conversation).
    if pending.original_message_id and not Message.objects.filter(
        gmail_message_id=pending.original_message_id
    ).exists():
        Message.objects.create(
            ticket=ticket, direction=Message.DIRECTION_INBOUND,
            gmail_message_id=pending.original_message_id,
            in_reply_to=pending.in_reply_to, references=pending.references,
            from_email=pending.customer_email, subject=pending.subject,
            body_text=pending.body_text, body_html=pending.body_html,
            headers=pending.headers,
        )

    # 2) Re-apply the stored classification (no second AI call).
    result = ClassificationResult(
        category=pending.category, sub_topic=pending.sub_topic,
        confidence=pending.confidence or 0.0, extracted=dict(pending.extracted or {}),
        sentiment=pending.sentiment, language=pending.language,
        is_support_request=True, issue_summary=pending.issue_summary,
        requires_evidence=True, requires_agent=pending.requires_agent,
        category_ref=pending.category_ref, sub_topic_ref=pending.sub_topic_ref,
    )
    apply_to_ticket(ticket, result, classification_status=Ticket.CLS_CLASSIFIED)

    # 3) Ingest the customer's REPLY onto the ticket. Its attachments were already
    #    captured on the pending conversation, so don't re-store the blobs here.
    reply = dict(message)
    reply["thread_id"] = thread_id
    reply.pop("attachment_blobs", None)
    ingest_message(mailbox, reply, run_ignore_gate=False)

    # 4) Move the ACCUMULATED evidence files (from all replies) onto the ticket, then set the
    #    flags from a FULL scan of the ticket's attachments (MIME + extension) -- so a video
    #    whose content_type wasn't 'video/*' is still detected and the engine never re-asks.
    pending_atts = list(pending.attachments.all())
    for att in pending_atts:
        att.ticket = ticket
        att.pending = None
        att.save(update_fields=["ticket", "pending", "updated_at"])
    ticket.refresh_from_db()
    has_photo, has_video = _sync_evidence_flags(ticket)
    logger.info("ATTACHMENTS_RECEIVED ticket=%s count=%d has_photo=%s has_video=%s",
                ticket.ticket_id, ticket.attachments.count(), has_photo, has_video)
    logger.info("EVIDENCE_VALIDATED ticket=%s has_photo=%s has_video=%s", ticket.ticket_id,
                has_photo, has_video)
    if has_photo or has_video:
        files = [a.filename for a in ticket.attachments.all()]
        logger.info("EVIDENCE-DETECTED ticket=%s has_photo=%s has_video=%s files=%d",
                    ticket.ticket_id, has_photo, has_video, len(files))
        AuditLogEntry.objects.create(
            ticket=ticket, actor="system", event="attachment_received",
            detail={"files": files, "count": len(files)},
        )
        AuditLogEntry.objects.create(
            ticket=ticket, actor="system", event="evidence_received",
            detail={"has_photo": has_photo, "has_video": has_video, "files": files},
        )
    logger.info("TICKET-CREATED ticket=%s from_pending=%s has_photo=%s has_video=%s",
                ticket.ticket_id, pending.id, has_photo, has_video)
    ticket.refresh_from_db()

    _add_internal_note(ticket, "Additional evidence received from customer")
    pending.delete()

    # 4) Finalize: decide -> Care Panel find-or-create -> store (tracking link) ->
    #    internal tracking fallback -> confirmation (always with a tracking link now).
    _auto_decide(ticket)
    # Route A (auto-answer & close): the engine already sent the answer and marked the
    # ticket auto_resolved. Golden rule -- no Care Panel ticket and no "created"
    # confirmation. This is how an order-status reply ends as an AUTO-REPLY (Shipment
    # Tracking: ask order -> lookup -> send status), never a support ticket.
    if _is_auto_resolved(ticket):
        logger.info("ROUTE-A auto-resolved (promoted) ticket=%s -> no Care Panel, no M5.",
                    ticket.ticket_id)
        return ticket
    # Ticket EXISTS -> the confirmation MUST go out. Finalize steps are guarded inside the helper
    # so a Care Panel / tracking / media failure can never block the customer's confirmation.
    _finalize_and_confirm(ticket, "created")
    return ticket


def _is_local_base(base):
    """True if a base URL is localhost / an internal IP (never safe for customer mail)."""
    import ipaddress
    from urllib.parse import urlparse

    if not base:
        return True
    host = (urlparse(base if "://" in base else "//" + base).hostname or "").lower()
    if host in ("", "localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified
    except ValueError:
        return False   # a real domain name -> public, fine


# The EXTERNAL Care Panel domains. Their /t page resolves only Care-Panel ticket
# hashes -- never our internal Django hashes -- so we must NOT build internal links
# there (that is the 404 cause).
CARE_PANEL_HOSTS = ("care.deodap.in", "care.deodap.info")

CARE_PANEL_TRACKING_URL_BASE = "https://care.deodap.in/t?id="

def _is_care_panel_url(url):
    return bool(url and _host_of(url) in CARE_PANEL_HOSTS and "/t?id=" in url)

def _care_panel_ticket_id_from_url(url):
    if not _is_care_panel_url(url):
        return ""
    return url.split("id=")[-1] if "id=" in url else ""

def _care_panel_tracking_url(ticket):
    extracted = ticket.extracted or {}
    hash_id = extracted.get("care_panel_ticket_id")
    # Only derive a Care Panel hash from the URL when this is NOT our internal fallback.
    # A care.deodap.in URL carrying an internal_tracking hash is the stale 404 case and
    # must NOT be treated as a real Care Panel link.
    if not hash_id and not extracted.get("internal_tracking"):
        hash_id = _care_panel_ticket_id_from_url(ticket.tracking_url or "")
    return f"{CARE_PANEL_TRACKING_URL_BASE}{hash_id}" if hash_id else ""

def _is_internal_tracking_url(url):
    if not url:
        return False
    if _is_local_base(url):
        return True
    from django.conf import settings
    public_base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    return bool(public_base and url.startswith(public_base))


def _host_of(base):
    from urllib.parse import urlparse

    return (urlparse(base if "://" in base else "//" + base).hostname or "").lower()


def portal_base_url():
    """Base URL of THIS Django app, where the /t tracking portal is served.

    Returns "" (no link) only when PUBLIC_BASE_URL is UNSET (the safe default, so we
    never accidentally email a localhost link) or points at the EXTERNAL Care Panel
    (which can't resolve our hashes). An EXPLICITLY-set address is trusted -- including a
    localhost/LAN address for dev/testing -- with a warning that it won't reach remote
    customers."""
    from django.conf import settings

    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    if not base:
        return ""
    # Reject ONLY the external Care Panel host (care.deodap.in) -- it can't resolve OUR hashes
    # (-> 404). Our own app (care.deodap.info/email_automation) is a valid /t portal base.
    if _host_of(base) == "care.deodap.in":
        logger.error("PUBLIC_BASE_URL %r is the EXTERNAL Care Panel domain, which cannot "
                     "resolve our internal hashes (-> 404). Set it to the public URL of "
                     "THIS Django app (the /t portal host).", base)
        return ""
    if _is_local_base(base):
        logger.warning("PUBLIC_BASE_URL %r is a localhost/LAN address -- the tracking link "
                       "will only open on this machine/network, not for remote customers.",
                       base)
    return base


def build_tracking_url(ticket=None, *, hash_id=None, ticket_id=""):
    """Build the public Care Panel tracking link -- ONLY from a REAL Care Panel hash
    (the `data.hash` returned by store-json):  https://care.deodap.in/t?id=<careHash>.

    An INTERNAL Django hash (tracking_hash / sha1) is NEVER wrapped in a care.deodap.in
    URL, because care.deodap.in cannot resolve it (-> 404). A ticket without a real Care
    Panel hash therefore gets NO link (the no-link confirmation variant), never a broken
    one. The host is hard-coded, so a localhost / 127.0.0.1 / 192.168.x / 10.x / 172.x
    address can never appear either.

    A hash_id passed explicitly is taken to be a real Care Panel hash (the caller
    asserts it). Derived from a ticket, only extracted.care_panel_ticket_id qualifies."""
    if hash_id is None and ticket is not None:
        hash_id = (ticket.extracted or {}).get("care_panel_ticket_id")
        ticket_id = ticket_id or ticket.ticket_id
    if not hash_id:
        logger.info("TRACKING_URL_SKIPPED ticket=%s reason=no_care_panel_hash -- internal "
                    "hash NOT emitted (care.deodap.in would 404); no link sent.",
                    ticket_id or "?")
        return ""
    url = f"{CARE_PANEL_TRACKING_URL_BASE}{hash_id}"
    logger.info("TRACKING_URL_GENERATED ticket=%s url=%s", ticket_id or "?", url)
    return url


def customer_ticket_link(ticket):
    """The customer-facing ticket link -> OUR /t portal (which shows the full Conversation), so
    customers land on a page we control. Ensures a resolvable tracking hash. Returns "" only when
    no portal base is configured (PUBLIC_BASE_URL unset / the external Care Panel), letting the
    caller fall back to a real Care Panel link."""
    base = portal_base_url()
    if not base:
        return ""
    extracted = dict(ticket.extracted or {})
    hash_id = (extracted.get("tracking_hash") or extracted.get("care_panel_ticket_id")
               or _tracking_hash(ticket))
    if extracted.get("tracking_hash") != hash_id:
        extracted["tracking_hash"] = hash_id
        ticket.extracted = extracted
        ticket.save(update_fields=["extracted", "updated_at"])
    return f"{base}/t?id={hash_id}"


def _is_bad_internal_link(ticket):
    """True if ticket.tracking_url is a care.deodap.in link carrying OUR internal hash
    instead of a REAL Care Panel hash -- care.deodap.in 404s on it, so it must be cleared
    (the ticket gets no link until store-json returns a real data.hash). A real Care
    Panel link (hash == care_panel_ticket_id) is fine and kept."""
    url = ticket.tracking_url or ""
    if not url or _host_of(url) not in CARE_PANEL_HOSTS:
        return False
    care_hash = (ticket.extracted or {}).get("care_panel_ticket_id")
    return not (care_hash and f"id={care_hash}" in url)


def _tracking_hash(ticket):
    import hashlib

    return hashlib.sha1(f"{ticket.ticket_id}:{ticket.brand_id}".encode()).hexdigest()[:10]


def _context_tracking_url(ticket):
    """A tracking URL templates can use. Customers now land on OUR /t portal (which shows the
    full Conversation); fall back to a real Care Panel link only when our portal isn't configured
    (or PUBLIC_BASE_URL points at the external Care Panel)."""
    ours = customer_ticket_link(ticket)
    if ours:
        return ours
    care_url = _care_panel_tracking_url(ticket)
    if care_url:
        extracted = dict(ticket.extracted or {})
        care_hash = extracted.get("care_panel_ticket_id") or _care_panel_ticket_id_from_url(
            ticket.tracking_url or ""
        )
        if care_hash:
            if extracted.get("care_panel_ticket_id") != care_hash:
                extracted["care_panel_ticket_id"] = care_hash
            if extracted.get("tracking_hash") != care_hash:
                extracted["tracking_hash"] = care_hash
            ticket.extracted = extracted
            ticket.save(update_fields=["extracted", "updated_at"])
        return care_url

    if ticket.tracking_url and not _is_local_base(ticket.tracking_url) \
            and not _is_bad_internal_link(ticket):
        return ticket.tracking_url
    # No REAL Care Panel hash -> no resolvable link (an internal hash on care.deodap.in
    # would 404). Keep the internal tracking_hash on record for our own /t portal, but
    # emit no care.deodap.in link.
    extracted = dict(ticket.extracted or {})
    hash_id = (extracted.get("tracking_hash") or extracted.get("care_panel_ticket_id")
               or _tracking_hash(ticket))
    if extracted.get("tracking_hash") != hash_id:
        extracted["tracking_hash"] = hash_id
        ticket.extracted = extracted
        ticket.save(update_fields=["extracted", "updated_at"])
    return build_tracking_url(ticket)


def _ensure_tracking(ticket):
    """Guarantee every ticket has ticket_number + tracking_hash + tracking_url.

    If the Care Panel created the ticket we keep its link (and record its hash);
    otherwise we mint an INTERNAL tracking URL on OUR /t route so the link ALWAYS
    resolves (no more 404) and never depends on phone availability."""
    ticket.refresh_from_db()
    extracted = dict(ticket.extracted or {})

    # Always have a tracking hash: reuse an existing one, else the Care Panel hash, else mint.
    hash_id = extracted.get("tracking_hash") or extracted.get("care_panel_ticket_id")
    if not hash_id:
        hash_id = _tracking_hash(ticket)
        logger.info("TRACKING_HASH_CREATED ticket=%s hash=%s", ticket.ticket_id, hash_id)
    number = ticket.ticket_number or ticket.ticket_id

    fields = []
    if extracted.get("tracking_hash") != hash_id:
        extracted["tracking_hash"] = hash_id
        fields.append("extracted")
    if ticket.ticket_number != number:
        ticket.ticket_number = number
        fields.append("ticket_number")

    # Prefer a real Care Panel link when present, even if it was originally stored
    # on ticket.tracking_url or only on extracted.care_panel_ticket_id.
    care_url = _care_panel_tracking_url(ticket)
    if care_url:
        care_hash = _care_panel_ticket_id_from_url(care_url)
        if care_hash and extracted.get("care_panel_ticket_id") != care_hash:
            extracted["care_panel_ticket_id"] = care_hash
            fields.append("extracted")
        if care_hash and extracted.get("tracking_hash") != care_hash:
            extracted["tracking_hash"] = care_hash
            if "extracted" not in fields:
                fields.append("extracted")
        if ticket.tracking_url != care_url:
            ticket.tracking_url = care_url
            fields.append("tracking_url")
        if extracted.pop("internal_tracking", None) is not None and "extracted" not in fields:
            fields.append("extracted")
        if fields:
            ticket.extracted = extracted
            ticket.save(update_fields=[*dict.fromkeys(fields), "updated_at"])
        logger.info("TRACKING-TOKEN=%s", care_hash or hash_id or "-")
        logger.info("TRACKING-URL=%s", care_url)
        return care_url

    # Keep a REAL link (Care Panel link or a valid portal link); replace a localhost
    # link or a bad internal-on-Care-Panel link (the 404 case).
    if ticket.tracking_url and not _is_local_base(ticket.tracking_url) \
            and not _is_bad_internal_link(ticket):
        if fields:
            ticket.extracted = extracted
            ticket.save(update_fields=[*dict.fromkeys(fields), "updated_at"])
        return ticket.tracking_url

    # No REAL Care Panel hash -> NO link. An internal Django hash would 404 on
    # care.deodap.in, so build_tracking_url(ticket) returns "" and we store no link
    # (the confirmation falls back to the no-link variant) rather than a broken one.
    # The internal tracking_hash stays on record for our own /t portal.
    new_url = build_tracking_url(ticket)
    ticket.tracking_url = new_url
    fields = ["tracking_url", "ticket_number", "extracted"]
    if new_url:
        extracted["internal_tracking"] = True
    ticket.extracted = extracted
    ticket.save(update_fields=[*dict.fromkeys(fields), "updated_at"])
    logger.info("TRACKING-TOKEN=%s", (ticket.extracted or {}).get("care_panel_ticket_id")
                or hash_id or "-")
    logger.info("TRACKING-URL=%s", new_url or "(none -- no real Care Panel hash)")
    if new_url:
        AuditLogEntry.objects.create(
            ticket=ticket, actor="system", event="internal_tracking_generated",
            detail={"tracking_url": new_url, "ticket_number": number, "hash": hash_id},
        )
    return new_url


def _ticket_intent_text(ticket):
    """Subject + summary + latest inbound body -- so the no-ticket guard matches the customer's
    natural phrasing ('Add one more item to my order'), not only the taxonomy sub-topic label."""
    msg = (ticket.messages.filter(direction=Message.DIRECTION_INBOUND)
           .order_by("-created_at").first())
    body = (msg.body_text if msg else "") or ""
    return f"{ticket.subject or ''} {ticket.issue_summary or ''} {body}"


def _handle_no_ticket_subcategory(ticket):
    """HARD GUARD for Add/Update Items & Add/Update GST (Make Changes To Order): send the fixed
    auto-reply and CLOSE. NO existing-ticket lookup, NO append, NO create, NO Care Panel, NO
    tracking link, NO 'existing ticket' email."""
    from apps.decision import policy
    from apps.ingestion import guided_flows

    logger.warning("Skipping ticket lookup for %s -- auto-reply only (no ticket / no existing "
                   "check / no tracking).", ticket.sub_topic or "-")
    flow = policy.no_ticket_flow(f"{ticket.sub_topic or ''} {_ticket_intent_text(ticket)}")
    if flow == "gst_update":
        body, subject = guided_flows.GST_REPLY, guided_flows.GST_SUBJECT
    else:
        body, subject = guided_flows.ADD_ITEMS_REPLY, guided_flows.ITEMS_SUBJECT
    in_reply_to, references = _reply_threading_headers(ticket)
    msg = Message.objects.create(
        ticket=ticket, direction=Message.DIRECTION_OUTBOUND,
        from_email=ticket.mailbox.email_address if ticket.mailbox else "",
        to_email=ticket.customer_email, subject=subject, body_text=body,
        in_reply_to=in_reply_to, references=references, sent_at=timezone.now())
    try:
        send_reply(msg)
    except Exception:  # noqa: BLE001 -- best-effort
        logger.exception("No-ticket auto-reply send failed for %s", ticket.ticket_id)
    ticket.status = Ticket.STATUS_AUTO_RESOLVED
    ticket.save(update_fields=["status", "updated_at"])
    AuditLogEntry.objects.create(ticket=ticket, actor="system", event="auto_replied",
                                 detail={"no_ticket_subcategory": ticket.sub_topic})
    return ticket


def _finalize_new_ticket(ticket, result):
    """Apply a pre-computed classification to a freshly created ticket, then match /
    decide / sync / confirm. Used for emails that do NOT need deferred evidence."""
    from apps.classifier.service import apply_to_ticket
    from apps.decision import policy

    if result is not None:
        apply_to_ticket(ticket, result, classification_status=Ticket.CLS_CLASSIFIED)
    else:
        ticket.classification_status = Ticket.CLS_FAILED
        ticket.save(update_fields=["classification_status", "updated_at"])
    ticket.refresh_from_db()
    if ticket.is_ignored:
        return ticket
    # HARD GUARD (Add/Update Items / GST): auto-reply only -- skip the existing-ticket lookup.
    if policy.blocks_ticket(ticket.category, ticket.sub_topic, _ticket_intent_text(ticket)):
        return _handle_no_ticket_subcategory(ticket)

    surviving = match_and_merge(ticket)
    if surviving is not None:
        AuditLogEntry.objects.create(
            ticket=surviving, actor="system", event="ticket_updated",
            detail={"reason": "matched_existing"},
        )
        _auto_decide(surviving)
        if _is_auto_resolved(surviving):
            return surviving           # Route A: answered & closed, no Care Panel / M5
        _sync_external(surviving)
        # If the surviving ticket never got a real Care Panel link (store-json failed
        # earlier and it's on an internal fallback), try again now so the "Existing
        # Ticket Found" mail carries a working care.deodap.in link.
        if not (surviving.extracted or {}).get("care_panel_ticket_id"):
            _store_care_panel(surviving)
            _ensure_tracking(surviving)
        send_confirmation(surviving, "updated")
        return surviving

    _auto_decide(ticket)
    # Route A (auto-answer & close): the engine already sent the M4 reply and set the
    # ticket auto_resolved. Golden rule -- no Care Panel ticket, no M5 confirmation.
    if _is_auto_resolved(ticket):
        logger.info("ROUTE-A auto-resolved ticket=%s -> no Care Panel ticket, M4 sent.",
                    ticket.ticket_id)
        return ticket
    # Ticket EXISTS -> the confirmation MUST go out (guarded finalize; see _finalize_and_confirm).
    _finalize_and_confirm(ticket, "created")
    return ticket


def _is_auto_resolved(ticket):
    """Route A outcome: the Mail Engine answered & closed via APIs/playbook -> the
    case lives only on the mail side (auto_resolved), never as a Care Panel ticket."""
    ticket.refresh_from_db()
    return ticket.status == Ticket.STATUS_AUTO_RESOLVED


def _escalation_search_text(message):
    """All text an escalation keyword may hide in: subject + body + attachment OCR / PDF text
    (whatever the ingest pipeline already extracted -- searched only when present). EMAIL
    ADDRESSES are stripped first so a sender like 'owner@shop.com' or 'press@x.com' never
    false-triggers a bare keyword (OWNER / PRESS)."""
    parts = [message.get("subject") or "", message.get("body_text") or message.get("snippet") or ""]
    parts.append(message.get("attachment_text") or message.get("ocr_text") or "")
    for blob in (message.get("attachment_blobs") or []):
        parts.append(blob.get("text") or blob.get("ocr_text") or "")
    return _EMAIL_RE.sub(" ", "\n".join(p for p in parts if p))


def _reply_refs(message):
    """The thread ids on an incoming email: In-Reply-To + every References id."""
    ids = []
    irt = (message.get("in_reply_to") or "").strip()
    if irt:
        ids.append(irt)
    refs = message.get("references")
    if isinstance(refs, str):
        ids += [r for r in refs.replace(",", " ").split() if r]
    elif isinstance(refs, (list, tuple)):
        ids += [str(r) for r in refs if r]
    return ids


def _reply_threads_into_ticket(brand, message):
    """True if this reply threads (In-Reply-To / References) into an EXISTING ticket -- so it is a
    ticket-thread reply, not an escalation continuation."""
    tid = message.get("thread_id") or _resolve_thread_id(brand, message)
    return bool(tid) and Ticket.objects.filter(brand=brand, thread_id=tid).exists()


def _maybe_escalate(mailbox, message, gmid):
    """Escalation gate -- runs BEFORE all automation. Returns True (escalated / appended), False
    (already recorded), or None (no escalation -> normal processing). When it returns non-None,
    NOTHING else runs: no classification, verification, tracking, evidence, pending, reminder,
    resolution or customer notification, and NO ticket."""
    from apps.tickets.models import Escalation

    # 1) A REPLY in an existing escalation thread -> append to it (keep it in the queue), never
    #    re-classify or create a ticket. Customer replies continue in the SAME escalation.
    refs = _reply_refs(message)
    if refs:
        existing = (Escalation.objects.filter(brand=mailbox.brand)
                    .filter(thread_ids__overlap=refs).first()
                    if _jsonfield_supports_overlap() else
                    _match_escalation_by_refs(mailbox.brand, refs))
        if existing is not None:
            _append_escalation_customer_reply(existing, message, gmid)
            return True

    # 1b) SENDER fallback: a customer REPLY whose Message-IDs didn't line up (agent replied from a
    #     different alias, multiple sends, or Gmail rewrote the Message-ID) is still a continuation
    #     -> append it to this sender's OPEN escalation instead of spawning a brand-new one. Only
    #     matches actively-open escalations (manual review / awaiting reply / pending) for the
    #     sender; never a resolved / ignored / already-ticketed one.
    sender = (message.get("from_email") or "").strip()
    is_reply = bool(refs) or (message.get("subject") or "").strip().lower().startswith("re:")
    if sender and is_reply:
        # GUARD: never hijack a reply that actually threads (by In-Reply-To / References) into a
        # more-specific conversation -- an evidence/verification PENDING or an existing TICKET. The
        # sender-fallback is ONLY for replies whose headers don't line up with anything else.
        # Without this, a customer who happens to have one open escalation has EVERY "Re:" reply --
        # including their evidence/photo+video upload to a DIFFERENT complaint -- swallowed into that
        # escalation, so the evidence pending stays stuck at waiting_for_video and never becomes a
        # ticket. (Reported: "wrong products" evidence reply routed to a "defective" escalation.)
        pending_match = _match_pending(mailbox.brand, message)[0]
        if pending_match is not None or _reply_threads_into_ticket(mailbox.brand, message):
            logger.info("ESCALATION-SENDER-FALLBACK-SKIP sender=%s message=%s -- reply threads into "
                        "a %s; NOT hijacking into an open escalation.", sender, gmid,
                        "pending conversation" if pending_match is not None else "ticket thread")
        else:
            open_esc = (Escalation.objects.filter(brand=mailbox.brand, sender__iexact=sender,
                                                  status__in=[Escalation.STATUS_MANUAL_REVIEW,
                                                              Escalation.STATUS_AWAITING_REPLY,
                                                              Escalation.STATUS_PENDING])
                        .order_by("-created_at").first())
            if open_esc is not None:
                logger.info("ESCALATION-REPLY-SENDER-MATCH escalation=%s sender=%s (refs didn't "
                            "match; matched by sender)", open_esc.id, sender)
                _append_escalation_customer_reply(open_esc, message, gmid)
                return True

    from apps.decision import policy
    keyword = policy.escalation_keyword(_escalation_search_text(message))
    if not keyword:
        return None

    sender = (message.get("from_email") or "").strip()
    subject = (message.get("subject") or "").strip()
    logger.info("ESCALATION-DETECTED")
    logger.info("ESCALATION-KEYWORD=%s", keyword)
    logger.info("SENDER=%s", sender or "-")
    logger.info("SUBJECT=%s", subject or "-")
    logger.info("MESSAGE_ID=%s", gmid or "-")
    logger.info("ACTION=MANUAL_REVIEW")
    logger.warning("ESCALATION_DETECTED=True -> ALL automation stopped (no ticket, no auto-reply, "
                   "no verification/tracking/evidence/pending) message=%s keyword=%s",
                   gmid, keyword)

    if gmid and Escalation.objects.filter(message_id=gmid).exists():
        return False                                   # dedup: same email, same record
    body = message.get("body_text") or message.get("snippet") or ""
    body_html = message.get("body_html") or ""
    esc = Escalation(
        organization=mailbox.brand.organization, brand=mailbox.brand, mailbox=mailbox,
        sender=sender, sender_name=(message.get("from_name") or "").strip().strip('"'),
        subject=subject or _derive_subject(message), body=body,
        matched_keyword=keyword, message_id=gmid, received_at=timezone.now(),
        status=Escalation.STATUS_MANUAL_REVIEW, priority="high", queue="escalation",
        thread_ids=[gmid] if gmid else [])
    esc.add_event("received", actor=sender or "customer", keyword=keyword)
    esc.save()
    # Persist the ACTUAL file bytes (was metadata-only before -> empty url -> link wouldn't open).
    saved = _save_escalation_attachments(esc, message)
    esc.attachments = saved
    esc.conversation = [{"direction": "inbound", "body": body, "body_html": body_html,
                         "message_id": gmid, "from": sender,
                         "at": timezone.now().isoformat(), "attachments": saved}]
    esc.save(update_fields=["attachments", "conversation", "updated_at"])
    return True


def _jsonfield_supports_overlap():
    from django.conf import settings as dj
    return "postgresql" in (dj.DATABASES.get("default", {}).get("ENGINE", ""))


def _match_escalation_by_refs(brand, refs):
    """SQLite-safe lookup: find an escalation whose thread_ids intersect the email's references."""
    from apps.tickets.models import Escalation
    wanted = set(refs)
    for esc in Escalation.objects.filter(brand=brand).order_by("-created_at")[:200]:
        if wanted & set(esc.thread_ids or []):
            return esc
    return None


def _sender_addresses(message):
    """Every address that identifies the SENDER of an email: From, Return-Path, Sender (and the
    raw header variants). Lower-cased, de-duplicated. Used to detect our OWN outbound mail."""
    from email.utils import getaddresses

    headers = message.get("headers") or {}
    raw = [message.get("from_email") or "", message.get("sender") or "",
           message.get("return_path") or ""]
    for key in ("From", "Sender", "Return-Path", "Reply-To", "X-Original-Sender",
                "X-Google-Original-From"):
        v = headers.get(key)
        if v:
            raw.append(v)
    out = set()
    for _, addr in getaddresses([r for r in raw if r]):
        a = (addr or "").strip().lower().strip("<>")
        if "@" in a:
            out.add(a)
    return out


def primary_inbox_address(mailbox=None):
    """The address customer replies MUST land on -- used as Reply-To so a reply (and its evidence)
    always returns to the inbox we actually POLL, even when sent FROM a 'send as' alias.

    Priority: explicit REPLY_TO setting -> the authenticated IMAP account (IMAP_USER, guaranteed
    deliverable since it's the account we read) -> the brand's primary SupportEmail -> the mailbox
    label. We prefer IMAP_USER over the primary SupportEmail because a branded primary (e.g.
    care@deodap.com) may be a SEND-ONLY alias that never delivers back to the polled inbox."""
    from django.conf import settings

    explicit = (getattr(settings, "REPLY_TO", "") or "").strip()
    if explicit:
        return explicit.lower()
    imap_user = (getattr(settings, "IMAP_USER", "") or "").strip()
    if imap_user:
        return imap_user.lower()
    if mailbox is not None:
        try:
            from apps.brand_settings.models import SupportEmail
            primary = (SupportEmail.objects.filter(brand=mailbox.brand, is_primary=True,
                                                   is_active=True).values_list("email", flat=True)
                       .first())
            if primary:
                return primary.strip().lower()
        except Exception:  # noqa: BLE001
            pass
        if getattr(mailbox, "email_address", ""):
            return mailbox.email_address.strip().lower()
    return ""


def reply_from_address(mailbox=None):
    """The ACTUAL From address outbound replies are sent as -- the configured REPLY_FROM, else the
    brand's PRIMARY SupportEmail, else the SMTP login (IMAP_USER). NOT the admin/login user. This
    is what gets recorded as the reply's sender_email (a 'send mail as' alias when one is set)."""
    from django.conf import settings

    reply_from = (getattr(settings, "REPLY_FROM", "") or "").strip()
    if reply_from:
        return reply_from.lower()
    if mailbox is not None:
        try:
            from apps.brand_settings.models import SupportEmail
            primary = (SupportEmail.objects.filter(brand=mailbox.brand, is_primary=True,
                                                   is_active=True).values_list("email", flat=True)
                       .first())
            if primary:
                return primary.strip().lower()
        except Exception:  # noqa: BLE001
            pass
    return (getattr(settings, "IMAP_USER", "") or "").strip().lower()


def resolve_sender_email(mailbox, requested="", *, default=""):
    """Resolve the actual From address for an outbound reply. The agent MAY choose any of the
    brand's active SupportEmails (primary inbox or a 'send mail as' alias). A requested address is
    honored ONLY if it is an active SupportEmail for the brand (no arbitrary From injection); else
    we fall back to `default` (the mailbox that received the email) or reply_from_address()."""
    requested = (requested or "").strip().lower()
    allowed = set()
    if mailbox is not None:
        try:
            from apps.brand_settings.models import SupportEmail
            allowed = {e.strip().lower() for e in SupportEmail.objects.filter(
                brand=mailbox.brand, is_active=True).values_list("email", flat=True) if e}
        except Exception:  # noqa: BLE001
            allowed = set()
    if requested and requested in allowed:
        return requested
    default = (default or "").strip().lower()
    if default and (not allowed or default in allowed):
        return default
    return reply_from_address(mailbox)


def _matches_support_email(mailbox, message):
    """Return the matched address if the email was sent BY one of the brand's active SupportEmails
    (primary inbox or alias), else "". Dynamic -- the list lives in Settings, nothing hardcoded."""
    try:
        from apps.brand_settings.models import SupportEmail
        own = set(SupportEmail.objects.filter(brand=mailbox.brand, is_active=True)
                  .values_list("email", flat=True))
    except Exception:  # noqa: BLE001 -- never block ingestion on a config lookup
        own = set()
    if not own:
        return ""
    own = {e.strip().lower() for e in own if e}
    for addr in _sender_addresses(message):
        if addr in own:
            return addr
    return ""


def _internal_recipients(message):
    """All recipient addresses on the email (To + Cc + Bcc), lower-cased."""
    out = []
    for key in ("to", "cc", "bcc"):
        val = message.get(key) or ""
        out += [a.strip().lower() for a in val.replace(",", " ").replace(";", " ").split() if "@" in a]
    return out


def _internal_recipient_match(message):
    """The first recipient that is on the INTERNAL_RECIPIENTS list, else None."""
    from django.conf import settings as dj
    internal = set(getattr(dj, "INTERNAL_RECIPIENTS", []) or [])
    if not internal:
        return None
    for addr in _internal_recipients(message):
        if addr in internal:
            return addr
    return None


def _maybe_internal(mailbox, message, gmid):
    """INTERNAL recipient gate -- runs FIRST. Returns True (stored / appended), False (already
    recorded), or None (not internal -> normal processing). When non-None, NOTHING else runs."""
    from apps.tickets.models import InternalEmail

    # A reply within an existing internal thread -> append (never a new record / ticket).
    refs = _reply_refs(message)
    if refs:
        existing = next((ie for ie in InternalEmail.objects.filter(brand=mailbox.brand)
                         .order_by("-created_at")[:200] if set(refs) & set(ie.thread_ids or [])),
                        None)
        if existing is not None:
            _append_internal_reply(existing, message, gmid)
            return True

    matched = _internal_recipient_match(message)
    if not matched:
        return None

    sender = (message.get("from_email") or "").strip()
    subject = (message.get("subject") or "").strip()
    logger.info("INTERNAL-EMAIL-DETECTED")
    logger.info("INTERNAL_RECIPIENT=True matched=%s from=%s subject=%s message_id=%s",
                matched, sender or "-", subject or "-", gmid or "-")
    logger.warning("INTERNAL-EMAIL-DETECTED matched=%s -> routed to Internal Communications "
                   "(NO ticket / auto-reply / escalation / verification).", matched)

    if gmid and InternalEmail.objects.filter(message_id=gmid).exists():
        return False
    body = message.get("body_text") or message.get("snippet") or ""
    atts = []  # incoming attachments saved below after the row exists
    ie = InternalEmail(
        organization=mailbox.brand.organization, brand=mailbox.brand, mailbox=mailbox,
        sender=sender, sender_name=(message.get("from_name") or "").strip().strip('"'),
        to_addrs=_internal_recipients(message), matched_recipient=matched,
        subject=subject or _derive_subject(message), body=body, message_id=gmid,
        received_at=timezone.now(), status=InternalEmail.STATUS_INTERNAL_REVIEW, priority="normal",
        thread_ids=[gmid] if gmid else [])
    ie.add_event("received", actor=sender or "internal")
    ie.save()
    saved = _save_internal_attachments(ie, message)
    ie.conversation = [{"direction": "inbound", "body": body,
                        "body_html": message.get("body_html") or "", "message_id": gmid,
                        "from": sender, "at": timezone.now().isoformat(), "attachments": saved}]
    ie.attachments = saved
    ie.save(update_fields=["conversation", "attachments", "updated_at"])
    return True


def _save_internal_attachments(internal_email, message):
    from django.core.files.base import ContentFile

    from apps.tickets.models import Attachment

    out = []
    for blob in (message.get("attachment_blobs") or []):
        content = blob.get("content")
        if not content:
            continue
        att = Attachment(internal_email=internal_email, filename=blob.get("filename") or "attachment",
                         content_type=blob.get("mime_type") or "", size=len(content))
        att.file.save(att.filename, ContentFile(content), save=False)
        att.save()
        out.append({"filename": att.filename, "content_type": att.content_type,
                    "url": f"/api/attachments/{att.id}/"})
    return out


def _append_internal_reply(internal_email, message, gmid):
    """A reply landed in an existing internal thread -> store it (no automation)."""
    from apps.tickets.models import InternalEmail

    body = message.get("body_text") or message.get("snippet") or ""
    saved = _save_internal_attachments(internal_email, message)
    convo = list(internal_email.conversation or [])
    convo.append({"direction": "inbound", "body": body, "body_html": message.get("body_html") or "",
                  "message_id": gmid, "from": message.get("from_email") or "",
                  "at": timezone.now().isoformat(), "attachments": saved})
    ids = list(internal_email.thread_ids or [])
    if gmid and gmid not in ids:
        ids.append(gmid)
    internal_email.conversation = convo
    internal_email.thread_ids = ids
    internal_email.is_read = False
    if internal_email.status not in InternalEmail.TERMINAL_STATUSES:
        internal_email.status = InternalEmail.STATUS_INTERNAL_REVIEW
    internal_email.add_event("reply_received", actor=message.get("from_email") or "")
    internal_email.save(update_fields=["conversation", "thread_ids", "is_read", "status",
                                       "timeline", "updated_at"])
    logger.info("INTERNAL-EMAIL-REPLY-RECEIVED internal=%s from=%s",
                internal_email.id, message.get("from_email") or "-")


def send_composed_email(*, to, subject, body_text, body_html=None, from_email=None,
                        cc=None, bcc=None, attachments=None, mailbox=None,
                        in_reply_to="", references=None):
    """Send a NEW or REPLY human-composed email (Compose page) through the EXISTING SMTP sender
    (smtp_client.send_email) -- no ticket, no automation. The From is a validated brand
    SupportEmail alias (resolve_sender_email); Cc/Bcc and file attachments are supported. For a
    reply within a Compose thread, pass `in_reply_to` (the last Message-ID) and `references` (all
    thread Message-IDs) so the customer's client threads it (Gmail RFC threading). Returns the new
    Message-ID on success, None on failure (logged)."""
    from django.conf import settings

    if not (to or "").strip():
        logger.error("COMPOSE-SEND-FAILED reason=no_recipient subject=%r", subject)
        return None
    provider = getattr(settings, "EMAIL_PROVIDER", "imap")
    if provider != "imap":
        logger.warning("COMPOSE-SEND-SKIPPED provider=%s (not 'imap') -> no SMTP send.", provider)
        return None
    # Honor the chosen alias only if it's an active SupportEmail for the brand; else fall back.
    from_addr = resolve_sender_email(mailbox, from_email) if mailbox is not None \
        else ((from_email or "").strip().lower() or reply_from_address())
    try:
        from .smtp_client import send_email

        sent_id = send_email(
            to=to, subject=subject, body_text=body_text, body_html=body_html,
            from_addr=from_addr, cc=cc, bcc=bcc, attachments=attachments,
            in_reply_to=in_reply_to or "", references=references or None,
            auto_submitted=False,          # a real, human-composed email, not a system auto-reply
        )
        logger.info("COMPOSE-SEND-SUCCESS to=%s from=%s message_id=%s in_reply_to=%s",
                    to, from_addr, sent_id, in_reply_to or "-")
        return sent_id
    except Exception as exc:  # noqa: BLE001 -- surfaced to the agent via the API response
        logger.error("COMPOSE-SEND-FAILED to=%s from=%s subject=%r error=%r",
                     to, from_addr, subject, exc)
        return None


def _match_composed_thread(brand, message):
    """Return (composed_email, already_appended) when an incoming email is a reply to a Compose
    thread, else (None, False). A reply is matched by RFC headers ONLY (In-Reply-To / References
    against ComposedEmail.thread_refs) -- never by subject. `already_appended` is True when THIS
    message was already folded into the thread (a re-fetch) so the caller must not duplicate it."""
    from apps.tickets.models import ComposedEmail

    own_ids = [i for i in (
        (message.get("message_id") or "").strip(),
        (message.get("gmail_message_id") or "").strip(),
    ) if i]
    # Dedup: this exact message already recorded on some thread -> no-op re-fetch.
    for tok in own_ids:
        ce = ComposedEmail.objects.filter(brand=brand, thread_refs__contains=tok).first()
        if ce is not None:
            return ce, True
    # Match by the reply's In-Reply-To / References ids.
    for ref in _reply_refs(message):
        ce = ComposedEmail.objects.filter(brand=brand, thread_refs__contains=ref).first()
        if ce is not None:
            return ce, False
    return None, False


def _save_composed_attachments(composed, message):
    """Persist an incoming reply's file attachments against the Compose thread and return their
    [{filename, url, content_type}] for the conversation entry (downloadable, like every other
    stored attachment)."""
    from django.core.files.base import ContentFile

    from apps.tickets.models import Attachment

    out = []
    for blob in (message.get("attachment_blobs") or message.get("attachments") or []):
        content = blob.get("content")
        filename = blob.get("filename") or blob.get("name") or "attachment"
        ct = blob.get("mime_type") or blob.get("content_type") or ""
        if content:
            att = Attachment(composed_email=composed, filename=filename, content_type=ct,
                             size=len(content))
            att.file.save(filename, ContentFile(content), save=False)
            att.save()
            out.append({"filename": filename, "content_type": ct,
                        "url": f"/api/attachments/{att.id}/"})
        elif blob.get("url"):
            out.append({"filename": filename, "content_type": ct, "url": blob["url"]})
    return out


def _append_composed_reply(composed, message, gmid):
    """A customer reply threaded into a Compose email -> append it to the SAME conversation.
    NO ticket, NO escalation, NO new conversation (exactly the Gmail behaviour). Stores reply
    attachments, records the inbound message, registers its Message-ID for further replies, and
    marks the thread unread."""
    body_text = (message.get("body_text") or "").strip()
    body_html = (message.get("body_html") or "").strip()
    atts = _save_composed_attachments(composed, message)
    composed.add_message({
        "direction": "inbound",
        "from": (message.get("from_email") or "").strip(),
        "to": (message.get("to") or "").strip(),
        "subject": (message.get("subject") or "").strip(),
        "body_html": body_html,
        "body_text": body_text,
        "message_id": (message.get("message_id") or gmid or "").strip(),
        "in_reply_to": (message.get("in_reply_to") or "").strip(),
        "at": timezone.now().isoformat(),
        "attachments": atts,
    })
    # Also register the Gmail/IMAP dedup id so a re-fetch is recognised even if Message-ID is blank.
    if gmid and gmid not in (composed.thread_refs or ""):
        composed.thread_refs = f"{composed.thread_refs} {gmid}".strip()
    composed.is_read = False
    if composed.status == composed.STATUS_DRAFT:
        composed.status = composed.STATUS_SENT   # a reply implies the thread is live
    composed.save(update_fields=["conversation", "thread_refs", "is_read", "status", "updated_at"])
    logger.info("COMPOSE-REPLY-APPENDED composed=%s from=%s message=%s (no ticket, no escalation)",
                composed.id, message.get("from_email") or "-", gmid)


def send_internal_reply(internal_email, body, *, agent="agent", subject=None, to=None,
                        email_attachments=None, stored_attachments=None, forward=False,
                        from_email=None):
    """Reply (or FORWARD, to `to`) on an internal email, preserving the thread (In-Reply-To /
    References / Message-ID) and carrying attachments. NO ticket, NO customer automation.
    `from_email`: the agent's chosen sender (validated SupportEmail alias)."""
    from apps.tickets.models import InternalEmail

    sender_from = resolve_sender_email(internal_email.mailbox, from_email)
    last_id = (internal_email.thread_ids or [internal_email.message_id] or [""])[-1]
    recipient = (to or internal_email.sender or "").strip()
    subj = (subject or "").strip() or (
        f"{'Fwd' if forward else 'Re'}: {internal_email.subject}" if internal_email.subject
        else ("Fwd: internal email" if forward else "Re: internal email"))
    sent_id = _send_customer_email(recipient, subj, body, in_reply_to=last_id,
                                   references=list(internal_email.thread_ids or []),
                                   attachments=email_attachments or None, from_email=sender_from,
                                   reply_to=primary_inbox_address(internal_email.mailbox))
    failed = not sent_id
    convo = list(internal_email.conversation or [])
    convo.append({"direction": "outbound", "body": body, "message_id": sent_id, "to": recipient,
                  "in_reply_to": last_id, "agent": agent, "at": timezone.now().isoformat(),
                  "subject": subj, "from": sender_from, "forward": forward,
                  "attachments": stored_attachments or [], "failed": failed})
    ids = list(internal_email.thread_ids or [])
    if sent_id and sent_id not in ids:
        ids.append(sent_id)
    internal_email.conversation = convo
    internal_email.thread_ids = ids
    if not failed:
        internal_email.status = InternalEmail.STATUS_AWAITING_REPLY
        internal_email.draft = ""
    internal_email.add_event("forwarded" if forward else ("reply_failed" if failed else "reply_sent"),
                             actor=agent, to=recipient, message_id=sent_id)
    internal_email.save(update_fields=["conversation", "thread_ids", "status", "draft",
                                       "timeline", "updated_at"])
    logger.info("INTERNAL-EMAIL-REPLY %s agent=%s to=%s message_id=%s",
                "FAILED" if failed else "SENT", agent, recipient, sent_id or "-")
    return sent_id


def add_internal_note(internal_email, note, *, agent="agent"):
    """Internal note on an internal email -- panel-only, never emailed."""
    convo = list(internal_email.conversation or [])
    convo.append({"direction": "note", "body": note, "agent": agent,
                  "at": timezone.now().isoformat()})
    internal_email.conversation = convo
    internal_email.add_event("internal_note", actor=agent)
    internal_email.save(update_fields=["conversation", "timeline", "updated_at"])
    logger.info("INTERNAL-EMAIL-NOTE internal=%s agent=%s", internal_email.id, agent)
    return internal_email


def _save_escalation_attachments(escalation, message):
    """Persist a customer-reply email's attachments against the escalation and return their
    [{filename, url, content_type}] for the conversation history (downloadable, like agent
    attachments)."""
    from django.core.files.base import ContentFile

    from apps.tickets.models import Attachment

    out = []
    for blob in (message.get("attachment_blobs") or message.get("attachments") or []):
        content = blob.get("content")
        filename = blob.get("filename") or blob.get("name") or "attachment"
        ct = blob.get("mime_type") or blob.get("content_type") or ""
        if content:
            # Inline bytes -> persist as a downloadable Attachment row.
            att = Attachment(escalation=escalation, filename=filename, content_type=ct,
                             size=len(content))
            att.file.save(filename, ContentFile(content), save=False)
            att.save()
            out.append({"filename": filename, "content_type": ct,
                        "url": f"/api/attachments/{att.id}/"})
        elif blob.get("url"):
            # URL-referenced attachment (no inline bytes) -> keep the external link as metadata.
            out.append({"filename": filename, "content_type": ct, "url": blob["url"]})
    return out


def _append_escalation_customer_reply(escalation, message, gmid):
    """A customer reply landed in an existing escalation thread -> store it, surface it for the
    agent (back to MANUAL_REVIEW), NO automation."""
    from apps.tickets.models import Escalation

    body = message.get("body_text") or message.get("snippet") or ""
    sender = message.get("from_email") or ""
    atts = _save_escalation_attachments(escalation, message)
    convo = list(escalation.conversation or [])
    convo.append({"direction": "inbound", "body": body, "body_html": message.get("body_html") or "",
                  "message_id": gmid, "from": sender, "at": timezone.now().isoformat(),
                  "attachments": atts})
    ids = list(escalation.thread_ids or [])
    if gmid and gmid not in ids:
        ids.append(gmid)
    escalation.conversation = convo
    escalation.thread_ids = ids
    escalation.status = Escalation.STATUS_MANUAL_REVIEW     # needs the agent again
    escalation.is_read = False                             # a new reply -> mark unread
    escalation.add_event("customer_reply", actor=sender or "customer")
    escalation.save(update_fields=["conversation", "thread_ids", "status", "is_read",
                                   "timeline", "updated_at"])
    logger.info("ESCALATION_CUSTOMER_REPLY escalation=%s from=%s (queued for manual review)",
                escalation.id, sender or "-")


def send_escalation_reply(escalation, body, *, agent="agent", subject=None, email_attachments=None,
                          stored_attachments=None, from_email=None):
    """Agent replies to the escalation's customer, preserving the email thread (In-Reply-To /
    References / a fresh Message-ID) and carrying any file attachments. Stores the message (with
    its attachments) and moves the escalation to 'Awaiting Customer Reply'. Returns the sent
    Message-ID.

    `email_attachments`: list of (filename, bytes, content_type) sent on the email.
    `stored_attachments`: list of {filename, url, content_type} recorded in the conversation.
    `from_email`: the agent's chosen sender (validated SupportEmail alias)."""
    from apps.tickets.models import Escalation

    sender_from = resolve_sender_email(escalation.mailbox, from_email)
    last_id = (escalation.thread_ids or [escalation.message_id])[-1] if (
        escalation.thread_ids or escalation.message_id) else ""
    subj = (subject or "").strip() or (
        escalation.subject if escalation.subject.lower().startswith("re:")
        else f"Re: {escalation.subject}" if escalation.subject else "Re: your message")
    sent_id = _send_customer_email(
        escalation.sender, subj, body, in_reply_to=last_id,
        references=list(escalation.thread_ids or []), attachments=email_attachments or None,
        from_email=sender_from, reply_to=primary_inbox_address(escalation.mailbox))
    failed = not sent_id          # _send_customer_email returns None on any SMTP / send failure
    convo = list(escalation.conversation or [])
    convo.append({"direction": "outbound", "body": body, "message_id": sent_id,
                  "in_reply_to": last_id, "agent": agent, "at": timezone.now().isoformat(),
                  "subject": subj, "from": sender_from, "attachments": stored_attachments or [],
                  "failed": failed})
    ids = list(escalation.thread_ids or [])
    if sent_id and sent_id not in ids:
        ids.append(sent_id)
    escalation.conversation = convo
    escalation.thread_ids = ids
    if not failed:
        escalation.status = Escalation.STATUS_AWAITING_REPLY  # only after a REAL send
        escalation.draft = ""                                 # sent -> clear any saved draft
    escalation.add_event("reply_failed" if failed else "reply_sent", actor=agent,
                         message_id=sent_id)
    escalation.save(update_fields=["conversation", "thread_ids", "status", "draft",
                                   "timeline", "updated_at"])
    if failed:
        logger.error("ESCALATION_REPLY_FAILED to=%s agent=%s -- email NOT delivered (see the "
                     "SMTP-SEND-FAILED log line above for the exact cause).",
                     escalation.sender, agent)
    else:
        logger.info("ESCALATION_REPLY_SENT")
        logger.info("AGENT=%s", agent)
        logger.info("TO=%s", escalation.sender)
        logger.info("MESSAGE_ID=%s", sent_id)
    return sent_id


def add_escalation_note(escalation, note, *, agent="agent"):
    """Add an INTERNAL note to an escalation -- visible only in the Care Panel, NEVER emailed."""
    convo = list(escalation.conversation or [])
    convo.append({"direction": "note", "body": note, "agent": agent,
                  "at": timezone.now().isoformat()})
    escalation.conversation = convo
    escalation.add_event("internal_note", actor=agent)
    escalation.save(update_fields=["conversation", "timeline", "updated_at"])
    logger.info("ESCALATION_INTERNAL_NOTE escalation=%s agent=%s", escalation.id, agent)
    return escalation


def _derive_subject(message):
    """The ticket subject when the email has none: the FIRST meaningful line of the body, else
    'No Subject'. Classification never relies on the subject (it uses body + identifiers), so a
    blank subject can never push a ticket into a wrong fallback category."""
    subject = (message.get("subject") or "").strip()
    if subject:
        return subject
    body = message.get("body_text") or message.get("snippet") or ""
    for raw in body.splitlines():
        line = raw.strip()
        # Skip blanks, quoted text, and reply/forward headers -> the first REAL line of content.
        if line and not line.startswith(">") and not line.lower().startswith(
                ("on ", "sent from", "-----", "from:", "to:", "subject:")):
            return line[:120]
    return "No Subject"


def _claim_incoming(mailbox, message, gmid):
    """Atomically claim this incoming Message-ID for processing. Returns True if WE claimed it
    (proceed to handle), False if it was ALREADY handled -- a re-poll or a concurrent worker.

    The unique `ProcessedEmail.message_id` is the cross-worker lock: only the first INSERT
    succeeds; any duplicate delivery hits IntegrityError and is skipped safely. This is what
    makes handling (and therefore the auto-reply) exactly-once even for PENDING REPLIES, which
    create no Ticket/Message row the older dedup could catch."""
    from django.db import IntegrityError, transaction

    from apps.tickets.models import ProcessedEmail

    try:
        with transaction.atomic():
            ProcessedEmail.objects.create(
                message_id=gmid, mailbox=mailbox,
                thread_id=(message.get("thread_id") or "")[:255],
                from_email=(message.get("from_email") or "")[:255])
        return True
    except IntegrityError:
        return False


def _mark_processed_complete(gmid, *, auto_reply_sent=None):
    """Stamp the claimed ProcessedEmail as fully handled (after successful processing)."""
    from apps.tickets.models import ProcessedEmail

    fields = {"completed_at": timezone.now()}
    if auto_reply_sent is not None:
        fields["auto_reply_sent"] = auto_reply_sent
    ProcessedEmail.objects.filter(message_id=gmid).update(**fields)


def handle_incoming_email(mailbox, message):
    """Single entry point for an inbound email (Smart Ticket Management).

    Order: dedup -> pending-evidence reply -> reply to existing ticket -> NEW email
    (classify BEFORE creating a ticket; if evidence is required and missing, hold it
    as a PendingConversation and request evidence -- NO ticket, NO ticket id yet).

    Returns (ticket_or_None, message_or_None, created) -- ticket is None for a held
    pending conversation; `created` is True for anything new (incl. pending).
    """
    brand = mailbox.brand
    gmid = message.get("gmail_message_id") or message.get("message_id") or ""
    message["gmail_message_id"] = gmid
    # No-subject email: derive a meaningful subject from the body (else 'No Subject') so every
    # downstream step (ticket, pending, replies) has one. Classification is unaffected -- it
    # already reads the body + identifiers, never the subject alone.
    message["subject"] = _derive_subject(message)

    # 1) Dedup against processed messages AND pending conversations.
    if gmid:
        logger.info("DUPLICATE-CHECK message_id=%s", gmid)
        existing = Message.objects.filter(gmail_message_id=gmid).select_related("ticket").first()
        if existing:
            blobs = message.get("attachment_blobs") or []
            if blobs and not existing.stored_attachments.exists():
                _store_attachments(existing.ticket, existing, blobs)
            logger.info("SKIP-DUPLICATE-TICKET message=%s already ingested (ticket=%s).",
                        gmid, existing.ticket.ticket_id if existing.ticket else None)
            logger.info("REPLY-DECISION message=%s matched=duplicate_ticket auto_reply=SKIPPED "
                        "reason=already_processed (re-fetch no-op).", gmid)
            return existing.ticket, existing, False
        if PendingConversation.objects.filter(
            Q(original_message_id=gmid) | Q(last_message_id=gmid)
        ).exists():
            logger.info("EMAIL-SKIPPED message=%s already held as a PendingConversation.", gmid)
            logger.info("REPLY-DECISION message=%s matched=duplicate_pending auto_reply=SKIPPED "
                        "reason=already_processed (re-fetch no-op).", gmid)
            return None, None, False

    # 0) OWN SUPPORT EMAIL -> NEVER import. An email whose From / Return-Path / Sender matches an
    # active SupportEmail (the primary inbox OR a 'send mail as' alias) is OUR OWN outbound message
    # that Gmail kept a copy of -> importing it would duplicate threads, skew stats and loop. Fully
    # dynamic: the match list comes from Settings -> Support Emails (no hardcoded addresses).
    own = _matches_support_email(mailbox, message)
    if own:
        logger.info("SUPPORT-EMAIL-SELF-SKIP from=%s matched=%s message=%s -> NOT imported "
                    "(our own sent mail / alias).", message.get("from_email") or "-", own, gmid)
        logger.info("REPLY-DECISION message=%s auto_reply=SKIPPED reason=own_support_email "
                    "(our own outbound copy, never a customer reply).", gmid)
        return None, None, False

    # 0b) IDEMPOTENCY GUARD -- claim this Message-ID atomically so the SAME incoming email is
    # processed (and auto-replied) EXACTLY once, across re-polls AND concurrent workers. This is
    # the single choke point that also covers PENDING REPLIES, which create no Ticket/Message row
    # for the older dedup (step 1) to catch. A losing/duplicate claim skips safely -- no auto-reply.
    if gmid and not _claim_incoming(mailbox, message, gmid):
        logger.info("DUPLICATE_MESSAGE_DETECTED message_id=%s thread_id=%s from=%s", gmid,
                    message.get("thread_id") or "-", message.get("from_email") or "-")
        logger.info("PROCESSING_SKIPPED message_id=%s reason=already_handled_or_concurrent_worker",
                    gmid)
        return None, None, False
    logger.info("PROCESSING_STARTED message_id=%s thread_id=%s from=%s subject=%r", gmid,
                message.get("thread_id") or "-", message.get("from_email") or "-",
                (message.get("subject") or "")[:120])

    # 1a) INTERNAL RECIPIENT -> the Internal Communications inbox. Checked FIRST (before anything
    # else): an email to/cc/bcc an internal address NEVER enters the support pipeline -- no
    # ticket, auto-reply, escalation, verification, tracking, evidence or pending conversation.
    internal = _maybe_internal(mailbox, message, gmid)
    if internal is not None:
        logger.info("REPLY-DECISION message=%s matched=internal_recipient auto_reply=SKIPPED "
                    "reason=internal_communication (never enters support pipeline by design).", gmid)
        return None, None, internal

    # 1b) BLOCK / IGNORE gate -- a sender/domain/header matching an ACTIVE block-list entry is
    # IGNORED here, BEFORE classification, escalation, evidence, pending and auto-reply, so a
    # blocked sender never gets a reply and is never held. INACTIVE entries do NOT match (the
    # Unblock behavior) -> such mail falls through to the normal pipeline below. ingest_message
    # creates the Ignored ticket (visible in the Ignored tab) and runs no further automation.
    if ignore_gate.evaluate(brand, message).ignored:
        ticket, msg, created = ingest_message(mailbox, message)
        logger.info("REPLY-DECISION message=%s from=%s matched=block_list auto_reply=SKIPPED "
                    "reason=blocked_sender_ignored (before classification).", gmid,
                    message.get("from_email") or "-")
        return ticket, msg, created

    # 1b-ii) COMPOSE-THREAD REPLY -> the customer replied to an email an agent sent from the
    # Compose page. Matched by RFC headers (In-Reply-To / References) against the Compose thread's
    # Message-IDs. Fold it into that SAME conversation and STOP -- never an escalation, never a new
    # ticket / conversation (Gmail behaviour). Checked BEFORE pending/escalation so a compose reply
    # can never be hijacked by the keyword/sender escalation rules.
    composed, already = _match_composed_thread(brand, message)
    if composed is not None:
        if not already:
            _append_composed_reply(composed, message, gmid)
        logger.info("REPLY-DECISION message=%s matched=compose_thread composed=%s auto_reply=SKIPPED "
                    "reason=%s -> appended to conversation (no ticket, no escalation).",
                    gmid, composed.id, "duplicate_refetch" if already else "compose_reply")
        return None, None, (not already)

    # Routing priority for a reply: existing ticket / pending conversation ALWAYS win over the
    # High-Priority escalation engine. A reply that belongs to an ACTIVE pending (fraud /
    # verification / evidence / inquiry) naturally contains trigger words ("fraud", "fraudster",
    # refund, legal ...) that would otherwise trip the escalation keywords and hijack it -> no
    # ticket. So resolve the pending FIRST and let its own workflow (block 2) handle the reply.
    active_pending = _find_pending(brand, message)
    if active_pending is not None:
        _ex = active_pending.extracted or {}
        _fraud = _ex.get("inquiry_type") in ("FRAUD_PAYMENT", "FRAUD_ALERT")
        _kind = _ex.get("inquiry_type") or _ex.get("intent") or active_pending.status or "pending"
        if _fraud:
            logger.info("FRAUD_PENDING_FOUND pending=%s type=%s status=%s from=%s -- reply "
                        "belongs to an active Fraud workflow.", active_pending.id,
                        _ex.get("inquiry_type"), active_pending.status,
                        message.get("from_email") or "-")
        logger.info("%s pending=%s kind=%s -- the High-Priority engine will NOT intercept a "
                    "reply that already belongs to an active pending conversation.",
                    "ESCALATION_SKIPPED_ACTIVE_FRAUD" if _fraud
                    else "ESCALATION_SKIPPED_ACTIVE_PENDING", active_pending.id, _kind)

    # 1c) HIGH-PRIORITY ESCALATION (only when NOT a reply to an active pending): legal /
    # consumer-court / grievance / negative-review. STOPS ALL automation for a NEW manual-review
    # email; the customer gets NO automatic reply, NO ticket.
    if active_pending is None:
        esc = _maybe_escalate(mailbox, message, gmid)
        if esc is not None:
            logger.warning(
                "REPLY-DECISION message=%s from=%s matched=escalation auto_reply=SKIPPED "
                "reason=escalation_manual_review (no auto-reply by design).",
                gmid, message.get("from_email") or "-")
            return None, None, esc

    # 2) Reply to a pending (fraud / verification / evidence / inquiry) conversation?
    pending = active_pending
    if pending is not None:
        # A reply within the 7-day window revives an auto-closed case.
        _reopen_if_closed(pending)
        # Fold this reply's evidence + order id into the ACCUMULATED state so we never
        # re-ask for something a previous reply already provided (no more loop).
        _accumulate_pending(pending, message)
        # Diagnostic summary for the evidence-reply workflow (one block, easy to grep).
        _total, _imgs, _vids = _attachment_counts(message)
        _parts = (message.get("attachment_blobs") or []) + (message.get("attachments") or [])
        logger.info(
            "EVIDENCE-REPLY-RECEIVED message_id=%s matched_pending=YES pending=%s customer=%s "
            "attachments_found=%d (images=%d videos=%d) files=%s evidence_validation=%s "
            "has_photo=%s has_video=%s",
            gmid, pending.id, message.get("from_email") or "-", _total, _imgs, _vids,
            [(p.get("filename") or "?", p.get("mime_type") or "?") for p in _parts],
            "PASS" if pending.has_evidence else "PENDING/NONE",
            pending.has_photo, pending.has_video)

        # Guided Website/App + Account sub-topic flow -> advance its state machine. NEVER
        # falls into the evidence / verification / complaint gates below.
        if (pending.extracted or {}).get("guided_flow"):
            from apps.ingestion import guided_flows
            return guided_flows.handle_reply(mailbox, message, pending)

        # Dedicated INQUIRY conversation -> advance its multi-step flow. NEVER falls into the
        # evidence / verification / complaint gates below.
        if (pending.extracted or {}).get("intent") == "INQUIRY":
            return _handle_inquiry_reply(mailbox, message, pending)

        # Double Payment held conversation: re-parse the mobile + accumulate the screenshot; ask
        # ONLY for the missing item, and create the ticket once BOTH are present (+ verified).
        if (pending.extracted or {}).get("intent") == "DOUBLE_PAYMENT":
            return _handle_double_payment_pending(mailbox, message, pending)

        # Cancellation conversation: every reply is PARSED AFRESH. We extract the newest Order
        # Number / mobile / AWB / Registered Email from THIS reply and verify THAT -- never the
        # previously-stored (possibly invalid) value. Verified -> ticket + clear pending; not
        # verified -> keep the pending and ask again. No cached failures, no duplicate pending.
        if (pending.extracted or {}).get("intent") == "ORDER_CANCELLATION":
            from apps.classifier.rule_classifier import _extract_order_id, _extract_phone

            reply_text = f"{message.get('subject', '')} {message.get('body_text', '')}"
            new_order = _extract_order_id(reply_text) or ""
            new_phone = _extract_phone(reply_text) or ""
            _o, _p, new_email = _tracking_identifiers(
                message, exclude_emails=[mailbox.email_address, pending.customer_email])
            new_awb = _extract_awb(reply_text) or ""
            prev_identifier = (pending.order_id or pending.phone
                               or (pending.extracted or {}).get("awb") or "-")
            new_identifier = new_order or new_phone or new_email or new_awb
            logger.info("PREVIOUS_IDENTIFIER=%s", prev_identifier)
            logger.info("NEW_IDENTIFIER=%s", new_identifier or "-")

            if not new_identifier:
                # The reply carried NO identifier -> ask again; NEVER re-verify the old value.
                logger.info("PENDING_CONTINUES pending=%s reason=no_identifier_in_reply", pending.id)
                _send_cancel_lookup(mailbox, message, pending)
                return None, None, True

            # REPLACE the stored identifier with the newest value -- clears any stale order/AWB so
            # a fresh mobile/email/order is never dragged down by a previous invalid order number.
            ex = {**(pending.extracted or {})}
            ex["order_id"] = new_order
            ex.pop("awb", None)
            if new_awb:
                ex["awb"] = new_awb
            pending.order_id = new_order
            if new_phone:
                pending.phone = new_phone
            pending.extracted = ex
            pending.save(update_fields=["order_id", "phone", "extracted", "updated_at"])
            logger.info("IDENTIFIER_UPDATED prev=%s new=%s", prev_identifier, new_identifier)

            # AWB candidate: a labelled/alphanumeric AWB, else the bare number (order OR AWB).
            awb_candidate = new_awb or new_order or ""
            logger.info("VERIFYING_IDENTIFIER order=%s phone=%s email=%s awb=%s", new_order or "-",
                        new_phone or "-", new_email or "-", awb_candidate or "-")
            proceed, status, info, verified_awb = _verify_cancellation_identifier(
                brand, order_id=new_order, phone=new_phone, email=new_email, awb=awb_candidate)
            logger.info("VERIFICATION_RESULT status=%s proceed=%s", status, proceed)

            if proceed:
                ex = _stamp_verified_customer({**(pending.extracted or {})}, info)
                if verified_awb and not ex.get("awb"):
                    ex["awb"] = verified_awb
                pending.extracted = ex
                pending.save(update_fields=["extracted", "updated_at"])
                ticket = _promote_pending(mailbox, pending, message)   # creates ticket + clears pending
                logger.info("TICKET_CREATED ticket=%s identifier=%s (cancellation verified).",
                            ticket.ticket_id, ex.get("order_id") or verified_awb or new_identifier)
                return ticket, ticket.messages.order_by("created_at").last(), True

            logger.warning("TICKET_CREATION_SKIPPED_INVALID_ORDER pending=%s -- identifier not "
                           "verified; NO ticket, NO Care Panel, NO confirmation email.", pending.id)
            logger.info("PENDING_CONTINUES pending=%s status=%s", pending.id, status)
            _send_cancel_lookup(mailbox, message, pending, template="M_CANCEL_NOT_FOUND")
            return None, None, True

        # Evidence-category verification gate (STEP 4 / STEP 7) on a reply. VERIFY-SOFT: if
        # proof has now arrived, accept it (clear the flag, fall through to the evidence
        # gate). Otherwise verify the identifier the customer replied with: a MATCH -> NOW
        # ask for the proof; a NO-MATCH / no identifier -> re-ask for an identifier (never a
        # photo/video request until verified).
        if (pending.extracted or {}).get("awaiting_verification"):
            if pending.has_evidence:
                logger.info("EVIDENCE-DETECTED pending=%s -> verify-soft accept (proof "
                            "present), continuing.", pending.id)
                _clear_awaiting_verification(pending)
                # fall through to the evidence/promote gate below
            else:
                # ORDER-first-then-MOBILE-then-EMAIL detection of the reply's identifier (a bare
                # number is tried as an order first, and only as a mobile when it is 10 digits).
                proceed, status, info, v_order, v_phone, v_email = _verify_reply_identifier(
                    pending, brand, message,
                    exclude_emails=[mailbox.email_address, pending.customer_email])
                # Escape hatch: after MAX_VERIFY_ATTEMPTS with SOME identifier provided, stop
                # looping "could not verify" -- create the ticket anyway (flagged unverified)
                # and let an agent sort it out, so the customer is never trapped.
                attempts = ((pending.extracted or {}).get("verify_attempts") or 0) + 1
                has_identifier = bool(v_order or v_phone or v_email)
                escalate = has_identifier and attempts >= MAX_VERIFY_ATTEMPTS
                logger.info("IDENTIFIER-DETECTED category=order pending=%s order=%s mobile=%s "
                            "email=%s status=%s attempt=%s escalate=%s", pending.id,
                            v_order or "-", v_phone or "-", v_email or "-", status, attempts,
                            escalate)
                if proceed or escalate:
                    ex = _stamp_verified_customer({**(pending.extracted or {})}, info)
                    ex["verify_attempts"] = attempts
                    if proceed:
                        ex["verified"] = True                # verification succeeded
                    else:
                        ex["verify_unconfirmed"] = status   # not_found / no_identifier
                    pending.extracted = ex
                    pending.save(update_fields=["extracted", "updated_at"])
                    _clear_awaiting_verification(pending)     # never send another verification email
                    if proceed:
                        logger.info("VERIFICATION_COMPLETED pending=%s verified=True matched_by=%s "
                                    "order=%s customer=%s", pending.id, info.get("matched_by") or "-",
                                    info.get("order_id") or "-", info.get("customer_name") or "-")
                    logger.info("VERIFICATION-RESULT %s pending=%s | VERIFIED-ORDER-ID %s | "
                                "VERIFIED-CUSTOMER %s",
                                "verified" if proceed else "escalated_unverified", pending.id,
                                info.get("order_id") or "-", info.get("customer_name") or "-")
                    # Evidence category -> NOW ask for the proof. Non-evidence order category
                    # (refund / return / address / RTO / delivered-not-received) -> create now.
                    if _pending_evidence_level(pending) != evidence.EV_NONE:
                        logger.info("CONTINUE_NEXT_WORKFLOW pending=%s next=request_evidence", pending.id)
                        return _request_pending_evidence(mailbox, message, pending)
                    logger.info("CONTINUE_NEXT_WORKFLOW pending=%s next=create_ticket", pending.id)
                    ticket = _promote_pending(mailbox, pending, message)
                    return ticket, ticket.messages.order_by("created_at").last(), True
                # Still failing, under the attempt cap -> ask once more (no infinite loop).
                pending.extracted = {**(pending.extracted or {}), "verify_attempts": attempts}
                pending.save(update_fields=["extracted", "updated_at"])
                logger.info("TICKET-BLOCKED-VERIFICATION pending=%s status=%s attempt=%s -> "
                            "re-ask identifier.", pending.id, status, attempts)
                _send_verification_failed(pending)
                return None, None, True

        level = _pending_evidence_level(pending)            # none / photo / video
        logger.info("PENDING-GATE id=%s level=%s has_evidence=%s has_video=%s has_photo=%s "
                    "order_id=%s phone=%s email=%s has_identifier=%s",
                    pending.id, level, pending.has_evidence, pending.has_video,
                    pending.has_photo, bool(pending.order_id), bool(pending.phone),
                    bool(pending.customer_email), _has_identifier(pending))

        # Delivered-Item evidence gate: each sub-case has its OWN mandatory files + wording
        # (Damaged = video+photo, Non-working = video, Missing = video+POS photo, Wrong Product
        # = video+photo(+SKU text), Wrong Parcel = photos only, Defective = photo+video). This
        # applies ONLY to the six delivered-item cases; every other category keeps the generic
        # photo/video gate below unchanged.
        delivered_case = _pending_delivered_case(pending) if level != evidence.EV_NONE else None
        if delivered_case is not None:
            if not _pending_meets_case(pending, delivered_case):
                # PROGRESSIVE: acknowledge what just arrived and ask ONLY for the item(s) still
                # missing -- never re-send the full EV_* template, never re-ask for evidence already
                # received. Holds the conversation until every mandatory file is present.
                _send_progressive_evidence_request(mailbox, message, pending, delivered_case)
                return None, None, True
            # All mandatory files received -> fall through and create the ticket immediately.
        # Category-first evidence gate: VIDEO-mandatory needs a video (photo-only is not
        # enough); PHOTO categories accept a photo (or video); NONE needs no media.
        elif level == evidence.EV_VIDEO and not pending.has_video:
            _send_video_request(mailbox, message, pending)         # no/ photo-only -> need video
            return None, None, True
        elif level == evidence.EV_PHOTO and not pending.has_evidence:
            _send_photo_request(mailbox, message, pending)         # need a photo (video optional)
            return None, None, True
        if level != evidence.EV_NONE and pending.has_evidence:
            logger.info("EVIDENCE-DETECTED pending=%s has_photo=%s has_video=%s -> "
                        "SKIP-EVIDENCE-REQUEST (proof already received, never re-ask).",
                        pending.id, pending.has_photo, pending.has_video)
        # Order-mandatory AUTO-REPLY categories (e.g. Shipment Tracking) must have the
        # ORDER before we proceed -- a reply with only a phone/email is not enough. Keep
        # the SAME pending open and re-ask for the order instead of promoting it. This is
        # what stops a phone reply (then an order reply) from creating two tickets, and
        # keeps tracking in the ask-order -> lookup -> auto-reply flow (no ticket).
        # Shipment Tracking reply: look up by the identifier the customer just provided
        # (order / phone / registered email) and send live status. Handled entirely here
        # -- NO ticket, NO M5/M6, and the pending is closed only once status is sent.
        # Two-step verification inquiry reply -> verify + (on success) create the ticket.
        # Checked BEFORE shipment tracking (same reason as the first-email 4-verify gate):
        # a verify pending must never be re-routed into the tracking-lookup flow just
        # because the AI stored category code 1 on it.
        if (pending.extracted or {}).get("verify_kind"):
            return _handle_verification_pending(mailbox, message, pending)
        if _is_shipment_tracking(pending):
            return _handle_tracking_pending(mailbox, message, pending)
        if _pending_needs_order(pending):
            logger.info("PENDING-GATE id=%s needs order_id (auto-reply category) -> re-ask, "
                        "no ticket.", pending.id)
            _send_identity_request(mailbox, message, pending)
            return None, None, True
        # Evidence satisfied. Create the ticket as long as we have ANY identifier
        # (email / phone / order id) -- phone is NOT required (new rule). Only if we have
        # nothing at all do we ask for an identifier (M1).
        if not _has_identifier(pending):
            _send_identity_request(mailbox, message, pending)
            return None, None, True
        ticket = _promote_pending(mailbox, pending, message)
        return ticket, ticket.messages.order_by("created_at").last(), True

    # 3) Reply that threads into an existing ticket?
    # Gmail carries a native threadId; IMAP resolves one from In-Reply-To/References.
    thread_id = message.get("thread_id") or _resolve_thread_id(brand, message)
    message["thread_id"] = thread_id
    threaded = Ticket.objects.filter(brand=brand, thread_id=thread_id).first() if thread_id else None
    logger.info("THREAD-MATCH from=%s message_id=%s in_reply_to=%s references=%s "
                "existing_ticket_found=%s",
                message.get("from_email"), gmid, message.get("in_reply_to"),
                message.get("references"), threaded.ticket_id if threaded else None)
    # Safety: never thread into a ticket that belongs to a DIFFERENT customer.
    if threaded is not None and threaded.customer_email and message.get("from_email") \
            and threaded.customer_email.strip().lower() != message["from_email"].strip().lower():
        logger.warning("THREAD-MATCH rejected: thread %s belongs to %s, not sender %s "
                       "-> starting a new thread.", thread_id, threaded.customer_email,
                       message.get("from_email"))
        thread_id = message.get("message_id") or gmid
        message["thread_id"] = thread_id
        threaded = None
    if threaded is not None:
        ticket, msg, created = ingest_message(mailbox, message)
        if created and not getattr(ticket, "_created_now", False) and not ticket.is_ignored:
            # FLOW 1: a reply on an existing thread is classified (status / info / ack / new
            # issue) -- NEVER run through duplicate detection or emailed 'Existing Ticket Found'.
            process_existing_reply(ticket, message=message, mailbox=mailbox)
        return ticket, msg, created

    # 4-cod) CASH ON DELIVERY inquiry -> fixed 'online prepaid only' auto-reply + Auto Resolved.
    #        DeoDap is online-prepaid ONLY; COD is never available. NO ticket, NO manual review,
    #        NO pincode ask. Runs BEFORE classification so a COD email is never routed into the
    #        serviceability / pincode flow. De-duplicated per conversation.
    if _is_cod_inquiry(message):
        return _handle_cod_inquiry(mailbox, message, gmid)

    # 4-inquiry) DEDICATED INQUIRY WORKFLOW -- runs FIRST. Franchisee / Dropshipping / Company
    #            Profile / Invoice / Other business inquiries go into a multi-step conversation,
    #            NEVER the support / verification / complaint flow (no order verify, no M1, no
    #            registered-email / AWB ask, no ticket). Keyword-detected on subject + body.
    inquiry_type = _detect_inquiry(message)
    if inquiry_type:
        return _handle_inquiry_first_email(mailbox, message, inquiry_type)

    # 4) NEW email -> classify BEFORE creating a ticket.
    result = _classify_dict(brand, message)
    # Record the SENDER separately on every classified email (sender_name / sender_email) so
    # it is available for conversation history + reply routing, while the ticket customer
    # identity is the verified Shopify order owner.
    if result is not None:
        result.extracted = _capture_sender_identity(dict(result.extracted or {}), message)

    # 4-cancel) ORDER CANCELLATION has the HIGHEST priority -- it must never be routed
    #           into the damage / evidence workflow, even if the AI mis-classified it.
    if result is not None and result.is_support_request and _is_cancellation(message, result):
        return _handle_cancellation(mailbox, message, result)

    # 4-double-payment) DOUBLE PAYMENT / deducted twice: NEVER an immediate ticket. Progressive
    #   collection of Registered Mobile + Payment Screenshot -> verify -> ticket. Must run before
    #   the generic evidence gate (which would ask only for a photo, not the mobile).
    if result is not None and result.is_support_request and _is_double_payment(message, result):
        return _handle_double_payment_first_email(mailbox, message, result)

    # 4-track) Shipment Tracking: explicit-identifier-only. We NEVER auto-track from the
    #          sender's email address -- this is handled entirely here (bypassing the
    #          self-lookup below). Require an order id / phone / registered email in the
    #          body: with one -> live status; without -> hold a pending and ask.
    if result is not None and result.is_support_request and _is_shipment_tracking(result):
        return _handle_tracking_first_email(mailbox, message, result)

    # 4-guided) Website/App (cat 15) + Account (cat 14) sub-topic flows. Each is a bespoke
    #   multi-step state machine (verify -> collect -> evidence/ticket, or a guided auto-reply)
    #   and OWNS these sub-topics -- it must run before the generic self-lookup / evidence /
    #   verify-first gates below.
    if result is not None and result.is_support_request:
        from apps.ingestion import guided_flows

        flow_key = guided_flows.detect_flow(result, message)
        if flow_key:
            return guided_flows.start_flow(mailbox, message, result, flow_key)

    # 4-pre) Self-lookup (§3b/§6): resolve the order from the sender's contact before
    #        asking. Adopts a single match; sends M1 + holds when none/ambiguous and the
    #        intent needs an order. No-op when Shopify isn't configured. (NOT for tracking.)
    if result is not None and result.is_support_request:
        handled = _resolve_identity_or_request(mailbox, message, result)
        if handled is not None:
            return handled

    # 4-evidence-verify) Evidence categories (Damaged / Defective / Wrong / Missing /
    #   Quality / Quantity) must verify the customer against Shopify BEFORE we ask for proof
    #   (STEP 4 / STEP 7). VERIFY-SOFT exception: if proof is ALREADY attached we never trap
    #   the customer -- fall through to the evidence gate. With no proof yet: a Shopify MATCH
    #   -> proceed to ask for the proof; a NO-MATCH / no identifier -> ask for an identifier
    #   first (no photo/video request, no ticket).
    if result is not None and result.is_support_request:
        # Pass the email body so "delivered but not received" is detected even when the AI
        # issue_summary is terse -> a non-delivery dispute never enters the evidence gate.
        ev_level = _result_evidence_level(result, text=message.get("body_text", "") or "")
        if ev_level != evidence.EV_NONE and not _message_has_evidence(message):
            total, images, videos = _attachment_counts(message)
            logger.info("ATTACHMENT-DETECTED image_count=%d video_count=%d", images, videos)
            order_id, phone, email = _tracking_identifiers(
                message, exclude_emails=[mailbox.email_address, message.get("from_email")])
            # Fold in any identifier the upstream self-lookup (4-pre) already resolved for
            # this sender, so a customer whose identity ALREADY matched Shopify (the same way
            # tracking finds them) verifies for evidence too -- never re-asked.
            ex = result.extracted or {}
            order_id = order_id or ex.get("order_id") or ""
            phone = phone or ex.get("phone") or ""
            logger.info("IDENTIFIER-DETECTED category=evidence order=%s mobile=%s email=%s",
                        order_id or "-", phone or "-", email or "-")
            proceed, status, info = _verify_against_shopify(brand, order_id, phone, email)
            if not proceed:
                return _handle_evidence_verification_request(mailbox, message, result, status)
            # Stamp the VERIFIED Shopify customer name onto the classification so the pending
            # (and the promoted ticket / Care Panel) uses the real order owner.
            result.extracted = _stamp_verified_customer(dict(result.extracted or {}), info)
            logger.info("VERIFICATION-SUCCESS category=evidence status=%s -> ask for proof "
                        "(level=%s).", status, ev_level)

    # 4-evidence) CATEGORY-FIRST evidence gate (classification already done above).
    #   VIDEO  -> Defective / Missing / Wrong Item: a video is mandatory; no attachment
    #             OR photo-only both wait for the video.
    #   PHOTO  -> Damaged / quality: a photo is required (a video is optional).
    #   NONE   -> Tracking / Refund / Return / General: no media; fall through to 5.
    if result is not None and result.is_support_request:
        level = _result_evidence_level(result)
        # Delivered-Item cases carry their own mandatory-evidence rules + exact wording; when the
        # first email is missing any mandatory file, hold it and send the case-specific request.
        delivered_case = _result_delivered_case(result, message) if level != evidence.EV_NONE else None
        if delivered_case is not None:
            if not _message_meets_case(message, delivered_case):
                rule = evidence.DELIVERED_EVIDENCE_RULES[delivered_case]
                status = "waiting_for_video" if rule["video"] else "awaiting_evidence"
                logger.info("EVIDENCE-GATE new-email from=%s delivered_case=%s need_photo=%s "
                            "need_video=%s decision=%s", message.get("from_email"), delivered_case,
                            rule["photo"], rule["video"], status)
                pending = _create_pending(mailbox, message, result, status=status)
                # Record any PARTIAL evidence already on this first email (e.g. a photo when both
                # photo+video are required) so we never re-ask for what the customer already sent.
                _accumulate_pending(pending, message)
                if pending.has_photo or pending.has_video:
                    # First email already carries SOME evidence -> acknowledge it and ask only for
                    # the rest (progressive), rather than the full request listing every item.
                    _send_progressive_evidence_request(mailbox, message, pending, delivered_case)
                else:
                    _send_delivered_evidence_request(mailbox, message, pending, delivered_case)
                return None, None, True
            # First email already has all mandatory files -> fall through to create the ticket.
        elif level == evidence.EV_VIDEO and not _message_has_video(message):
            total, images, videos = _attachment_counts(message)
            logger.info("EVIDENCE-GATE new-email from=%s level=video attachments=%d "
                        "images=%d videos=%d category=%s decision=waiting_for_video",
                        message.get("from_email"), total, images, videos, result.category)
            pending = _create_pending(mailbox, message, result, status="waiting_for_video")
            _send_video_request(mailbox, message, pending)
            return None, None, True
        elif level == evidence.EV_PHOTO and not _message_has_evidence(message):
            logger.info("EVIDENCE-GATE new-email from=%s level=photo category=%s "
                        "decision=awaiting_evidence", message.get("from_email"), result.category)
            pending = _create_pending(mailbox, message, result)
            _send_photo_request(mailbox, message, pending)
            return None, None, True
        if level != evidence.EV_NONE:
            # Evidence IS present on this first email -> create the ticket as long as we
            # have any identifier (email/phone/order id). Phone is NOT required.
            pending = _create_pending(mailbox, message, result)
            _accumulate_pending(pending, message)
            if not _has_identifier(pending):
                _send_identity_request(mailbox, message, pending)
                return None, None, True
            ticket = _promote_pending(mailbox, pending, message)
            return ticket, ticket.messages.order_by("created_at").last(), True

    # 4-verify-first) VERIFICATION-FIRST RULE -- the customer must be verified (order number /
    #   registered mobile / registered email, OR-based) BEFORE ANY action for categories tied
    #   to an order or account: ticket categories AND verified-auto-reply categories (tracking /
    #   item-or-GST edits / offers / delete-account / data-privacy). Pure business inquiries
    #   (franchise / dropship / company / bulk -- handled earlier) and general pre-sale info
    #   (product / coverage / store info) + uncategorized skip this. After verification, the
    #   decision engine (step 5) routes to a ticket or an auto-reply per the policy taxonomy.
    #   NOTE: _verify_against_shopify returns proceed=True when Shopify is unconfigured/down
    #   (status 'cannot_verify') so we never trap a customer behind a broken integration.
    if result is not None and result.is_support_request:
        logger.info("ISSUE-CLASSIFIED category=%s sub_topic=%s", result.category or "-",
                    result.sub_topic or "-")
        needs_verification = _requires_verification(result)
        will_ticket = _result_requires_ticket(result, message)
        logger.info("VERIFICATION-REQUIRED %s | REQUIRES-TICKET %s", needs_verification,
                    will_ticket)
        if needs_verification:
            o, p, e = _tracking_identifiers(
                message, exclude_emails=[mailbox.email_address, message.get("from_email")])
            ex = result.extracted or {}
            o = o or ex.get("order_id") or ""
            p = p or ex.get("phone") or ""
            proceed, status, info = _verify_against_shopify(brand, o, p, e)
            logger.info("VERIFICATION-RESULT %s order=%s mobile=%s email=%s",
                        status, o or "-", p or "-", e or "-")
            if not proceed:
                logger.info("DECISION-ACTION block_send_verification (category=%s) -- verify "
                            "the customer before ANY action.", result.category or "-")
                return _handle_order_verification_request(mailbox, message, result, status)
            result.extracted = _stamp_verified_customer(dict(result.extracted or {}), info)
            logger.info("VERIFIED-ORDER-ID %s | VERIFIED-CUSTOMER %s | DECISION-ACTION %s",
                        info.get("order_id") or "-", info.get("customer_name") or "-",
                        "create_ticket" if will_ticket else "auto_reply")

    # 5) No evidence needed -> create the ticket now and finalize (decision engine
    #    handles Route A auto-answer, refund/return ticket, general -> agent, etc.).
    ticket, msg, created = ingest_message(mailbox, message)
    if created and getattr(ticket, "_created_now", True):
        logger.info("TICKET-CREATED ticket=%s category=%s (order_verified_or_non_order)",
                    getattr(ticket, "ticket_id", "?"), result.category if result else "-")
        _finalize_new_ticket(ticket, result)
    return ticket, msg, created


def fetch_imap(mailbox, client=None):
    """Pull only NEW mail over IMAP (UID > mailbox.imap_last_uid) into tickets, then
    classify + decide each new one. Old mail is never re-fetched.

    Dedup is twofold: UID-based (we only fetch unseen UIDs) plus RFC822 Message-ID
    (the unique gmail_message_id field). Returns the ingested results; callers count
    `created=True` for the "Fetched X new emails" message.
    """
    if client is None:
        from .imap_client import ImapClient

        client = ImapClient.from_settings()
    if client is None:
        logger.warning("fetch_imap skipped: IMAP not configured.")
        return []

    brand = mailbox.brand
    validity, items = client.fetch_new(
        last_uid=mailbox.imap_last_uid or 0,
        uidvalidity=mailbox.imap_uidvalidity,
    )

    results = []
    max_uid = mailbox.imap_last_uid or 0
    for uid, message in items:
        # Stable dedup key: rfc822 Message-ID, else the per-mailbox UID.
        message["gmail_message_id"] = message.get("message_id") or f"imap-uid-{uid}"
        message["imap_uid"] = uid
        logger.info("EMAIL-FETCHED uid=%s mailbox=%s", uid, mailbox.email_address)
        logger.info("EMAIL-PARSED from=%s subject=%r | MESSAGE-ID=%s | IN-REPLY-TO=%s | "
                    "REFERENCES=%s | EMAIL-THREAD-ID=%s",
                    message.get("from_email") or "-", (message.get("subject") or "")[:120],
                    message.get("message_id") or "-", message.get("in_reply_to") or "-",
                    message.get("references") or "-", message.get("thread_id") or "-")
        # Classify-before-create + evidence-deferral pipeline (Smart Ticket Mgmt).
        # Isolate per-message: a single failing email logs a FULL traceback (never swallowed
        # silently) and is skipped so it cannot abort the whole batch or block newer mail.
        try:
            ticket, msg, created = handle_incoming_email(mailbox, message)
        except Exception:  # noqa: BLE001 -- isolate one poison message, log loudly, continue
            logger.exception(
                "EMAIL-PROCESSING-FAILED uid=%s mailbox=%s message_id=%s from=%s subject=%r -- "
                "full traceback above; this email is skipped (no ticket created).",
                uid, mailbox.email_address, message.get("message_id") or "-",
                message.get("from_email") or "-", (message.get("subject") or "")[:120])
            max_uid = max(max_uid, uid)
            continue
        gmid = message.get("gmail_message_id") or ""
        logger.info("PROCESSING_COMPLETED message_id=%s thread_id=%s ticket=%s created=%s",
                    gmid, message.get("thread_id") or "-",
                    ticket.ticket_id if ticket else "-", created)
        if gmid:
            _mark_processed_complete(gmid)
        if ticket is not None:
            logger.info("INBOX-INSERT table=tickets_ticket ticket=%s status=%s -> VISIBLE in "
                        "ticket inbox.", ticket.ticket_id, ticket.status)
        elif created:
            logger.info("EMAIL-HIDDEN held as PendingConversation (no Ticket row) -> NOT in the "
                        "ticket inbox and NOT pushed to Care Panel; appears only under /pending.")
        else:
            logger.info("EMAIL-SKIPPED uid=%s -> duplicate / no-op (already ingested).", uid)
        results.append((ticket, msg, created))
        max_uid = max(max_uid, uid)

    # Advance the watermark so the next Fetch only sees newer mail.
    mailbox.imap_last_uid = max_uid
    if validity:
        mailbox.imap_uidvalidity = validity
    mailbox.save(update_fields=["imap_last_uid", "imap_uidvalidity", "updated_at"])

    new_count = sum(1 for _t, _m, created in results if created)
    logger.info("fetch_imap mailbox=%s new=%d (last_uid=%d)",
                mailbox.email_address, new_count, max_uid)
    return results


_TICKET_ID_RE = re.compile(r"\bTKT-\d{4}-\d{6}\b")


def _open_candidates(ticket):
    """Open, non-ignored tickets from the same customer (excluding this one)."""
    return (
        Ticket.objects.filter(
            brand=ticket.brand,
            customer_email__iexact=ticket.customer_email,
            is_ignored=False,
        )
        .exclude(pk=ticket.pk)
        .exclude(status__in=Ticket.TERMINAL_STATUSES)
        .order_by("created_at")
    )


def _first_inbound_text(ticket):
    m = (
        ticket.messages.filter(direction=Message.DIRECTION_INBOUND)
        .order_by("created_at").first()
    )
    return f"{m.subject}\n{m.body_text}" if m else ""


def _match_by_gallabox(ticket):
    """Priority 1: the email references a Gallabox ticket id we already track."""
    text = _first_inbound_text(ticket)
    tokens = set(re.findall(r"\bgb[-_][A-Za-z0-9]+\b", text or "", re.IGNORECASE))
    if not tokens:
        return None
    for c in _open_candidates(ticket):
        gid = (c.extracted or {}).get("gallabox_id")
        if gid and gid in tokens:
            return c
    return None


def _match_by_ticket_id(ticket):
    """Priority 2: the customer quoted an existing ticket id (TKT-YYYY-NNNNNN)."""
    for tid in _TICKET_ID_RE.findall(_first_inbound_text(ticket)):
        m = (
            Ticket.objects.filter(brand=ticket.brand, ticket_id=tid)
            .exclude(pk=ticket.pk)
            .exclude(status__in=Ticket.TERMINAL_STATUSES)
            .first()
        )
        if m:
            return m
    return None


def _same_issue_type(a, b):
    """True when two tickets are the SAME issue type (Care Panel issue).

    'Order Delayed' and 'Wrong Item' on the SAME order are DIFFERENT issues and must
    become separate tickets (#100 vs #101) -- not appended to each other. We prefer the
    explicit taxonomy sub-topic; when a ticket has none we fall back to the resolved
    Care Panel issue id, then to the coarse category. This mirrors the external Care
    Panel match (`care_panel._same_issue`) so local + remote agree on "same issue".
    """
    if a.sub_topic_ref_id and b.sub_topic_ref_id:
        return a.sub_topic_ref_id == b.sub_topic_ref_id
    if a.category and b.category and a.category != b.category:
        return False
    try:
        from apps.integrations.care_panel_store import resolve_issue

        return str(resolve_issue(a)[0]) == str(resolve_issue(b)[0])
    except Exception:  # noqa: BLE001 -- resolver is best-effort; same category stands
        return True


def _match_by_order_id(ticket):
    """Priority 3: same customer + same order_id + SAME ISSUE TYPE. A different issue
    on the same order is a NEW ticket (it is not appended to the existing one)."""
    order_id = (ticket.extracted or {}).get("order_id")
    if not order_id or not ticket.customer_email:
        return None
    for c in _open_candidates(ticket).filter(extracted__order_id=order_id):
        if _same_issue_type(ticket, c):
            return c
    return None


def _same_verified_customer(a, b):
    """False when two tickets have DIFFERENT verified phones (the order owner). The email SENDER
    is shared across customers (one Gmail can submit for many), so it is NOT a customer identity
    -- only the verified phone is. When either side has no phone we can't disprove it -> allow."""
    def d10(t):
        d = "".join(c for c in str((t.extracted or {}).get("phone") or "") if c.isdigit())
        return d[-10:] if len(d) >= 10 else ""
    pa, pb = d10(a), d10(b)
    return not (pa and pb and pa != pb)


def _match_by_similarity(ticket):
    """Priority 4: same category + AI says it's the same issue (conf > 0.8).
    Falls back to a same-sub-topic heuristic when no AI is available."""
    if not ticket.category_ref_id or not ticket.customer_email:
        return None
    from apps.classifier import service as classifier

    new_sum = (ticket.extracted or {}).get("issue_summary") or ticket.issue_summary or ticket.subject
    for c in _open_candidates(ticket).filter(category_ref=ticket.category_ref_id):
        if not _same_verified_customer(ticket, c):   # different order owner -> NOT the same ticket
            continue
        verdict = classifier.same_issue(ticket.brand, new_sum, c.issue_summary or c.subject)
        if verdict is not None:
            same, conf = verdict
            if same and conf > 0.8:
                return c
        elif ticket.sub_topic_ref_id and ticket.sub_topic_ref_id == c.sub_topic_ref_id:
            return c  # heuristic: same sub-topic = same issue
    return None


def _merge_into(ticket, target, reason):
    """Append this ticket's messages to `target`, audit it, drop the duplicate."""
    moved_from = ticket.ticket_id
    ticket.messages.update(ticket=target)
    AuditLogEntry.objects.create(
        ticket=target, actor="system", event="conversation_appended",
        detail={"from_ticket": moved_from, "reason": reason},
    )
    ticket.delete()
    logger.info("merged %s into %s (%s)", moved_from, target.ticket_id, reason)
    return target


def match_and_merge(ticket):
    """Ticket-matching priority chain (spec): explicit ticket id -> same order_id ->
    same category + issue similarity. (Same-thread is already handled at ingest.)
    Merges into the matched ticket and returns it, else None (-> stays a new ticket).
    """
    from apps.decision import policy

    # HARD GUARD: never look up / append to an existing ticket for the no-ticket sub-topics.
    if policy.blocks_ticket(ticket.category, ticket.sub_topic, _ticket_intent_text(ticket)):
        logger.warning("Skipping existing-ticket lookup for %s.", ticket.sub_topic or "-")
        return None
    for finder, reason in (
        (_match_by_gallabox, "gallabox"),
        (_match_by_ticket_id, "ticket_id"),
        (_match_by_order_id, "order_id"),
        (_match_by_similarity, "ai_similarity"),
    ):
        match = finder(ticket)
        if match:
            surviving = _merge_into(ticket, match, reason)
            if reason == "gallabox":
                AuditLogEntry.objects.create(
                    ticket=surviving, actor="system", event="gallabox_ticket_matched",
                    detail={"gallabox_id": (surviving.extracted or {}).get("gallabox_id")},
                )
            return surviving
    return None


def merge_order_duplicate(ticket):
    """Order-id-only match + merge (kept for callers/tests)."""
    c = _match_by_order_id(ticket)
    return _merge_into(ticket, c, "order_id") if c else None


def _add_internal_note(ticket, text):
    AuditLogEntry.objects.create(
        ticket=ticket, actor="system", event="internal_note", detail={"note": text},
    )


def _scan_ticket_evidence(ticket):
    """Scan ALL stored attachments for the conversation -> (has_photo, has_video),
    by MIME + file extension (jpg/jpeg/png/webp...  mp4/mov/avi/mkv/webm...)."""
    return evidence.scan_attachments(
        (a.filename, a.content_type) for a in ticket.attachments.all())


def _sync_evidence_flags(ticket):
    """Make ticket.extracted.has_photo / has_unboxing_video reflect the actual stored
    attachments, so NOTHING (incl. the decision engine) re-asks for evidence we already
    have. Returns (has_photo, has_video). Idempotent; only writes when it changes."""
    has_photo, has_video = _scan_ticket_evidence(ticket)
    extracted = dict(ticket.extracted or {})
    changed = False
    if has_photo and not extracted.get("has_photo"):
        extracted["has_photo"] = True
        changed = True
    if has_video and not extracted.get("has_unboxing_video"):
        extracted["has_unboxing_video"] = True
        changed = True
    if changed:
        ticket.extracted = extracted
        ticket.save(update_fields=["extracted", "updated_at"])
        logger.info("EVIDENCE-SCAN ticket=%s has_photo=%s has_video=%s "
                    "attachments=%d -> flags synced", ticket.ticket_id, has_photo,
                    has_video, ticket.attachments.count())
    return has_photo, has_video


def _has_evidence(ticket):
    extracted = ticket.extracted or {}
    if extracted.get("has_photo") or extracted.get("has_unboxing_video"):
        return True
    has_photo, has_video = _scan_ticket_evidence(ticket)
    return has_photo or has_video


def _suppress_internal_ref(body, ref):
    """Remove the 'Ticket ID: <ref>' paragraph from a rendered confirmation body. Used when there
    is NO real Care Panel number (e.g. an escalated / unverified ticket whose store-json was skipped
    for lack of a phone): we keep the acknowledgment + the tracking link but never show the internal
    TKT-... id to the customer. Language-agnostic -- it drops the whole line carrying `ref`, so it
    works across the en / hi / gu label variants ('Ticket ID:' / 'टिकट आईडी:' / 'ટિકિટ આઈડી:')."""
    if not ref:
        return body
    kept = [ln for ln in body.split("\n") if ref not in ln]
    text = "\n".join(kept)
    while "\n\n\n" in text:                 # collapse the blank gap left where the line was
        text = text.replace("\n\n\n", "\n\n")
    return text


def send_confirmation(ticket, kind):
    """Email the customer a ticket created / updated confirmation (Smart Ticket mgmt)."""
    from django.conf import settings

    from apps.decision import policy

    if not getattr(settings, "SEND_TICKET_CONFIRMATIONS", True):
        return None
    if ticket.is_ignored or not ticket.customer_email:
        return None
    # MANDATORY SAFETY CHECK: never send a 'ticket created' email for the no-ticket sub-topics.
    if kind == "created" and policy.blocks_ticket(ticket.category, ticket.sub_topic, _ticket_intent_text(ticket)):
        logger.warning("Blocked ticket creation for %s -- no 'created' confirmation sent.",
                       ticket.sub_topic or "-")
        return None

    logger.info("SEND_CONFIRMATION_START ticket=%s kind=%s to=%s", ticket.ticket_id, kind,
                ticket.customer_email)
    # EVERY ticket email must carry the tracking link -- new ticket, existing-ticket update, OR
    # duplicate-found-existing alike. Ensure the Care Panel ticket exists + tracking is
    # populated BEFORE composing. Idempotent: a no-op when a real Care Panel hash is already on
    # the ticket, so an update / duplicate reuses the SAME link the "created" email sent.
    # GUARDED: a Care Panel / tracking failure must NEVER stop the confirmation -- we fall back to
    # the no-link M5N variant rather than letting an exception abort the send (the reported bug:
    # ticket created, but the email never went out because a finalize step raised here).
    try:
        _store_care_panel(ticket)
        _ensure_tracking(ticket)
    except Exception:  # noqa: BLE001 -- never block the confirmation on link/store errors
        logger.exception("SEND_CONFIRMATION link/store prep failed for %s -- sending without a "
                         "fresh link.", ticket.ticket_id)
    ticket.refresh_from_db()
    logger.info("TRACKING_HASH ticket=%s hash=%s url=%s", ticket.ticket_id,
                (ticket.extracted or {}).get("tracking_hash") or "-",
                _care_panel_tracking_url(ticket) or ticket.tracking_url or "-")

    # Outbound From MUST be an authorized sender (SMTP-authenticated account or a configured
    # 'send mail as' alias), exactly like a manual reply -- NOT the raw mailbox label. Using the
    # raw ticket.mailbox.email_address made Gmail/SMTP refuse the sender, so the M5/M6
    # confirmation silently failed to send even though the ticket was created. reply_from_address()
    # resolves REPLY_FROM -> brand primary SupportEmail -> IMAP_USER (all deliverable).
    confirm_from = reply_from_address(ticket.mailbox)

    # A guided flow (e.g. Make Changes To Order -> Update Address) supplies its OWN exact
    # confirmation wording/subject; send that (still WITH the tracking link) instead of M5.
    gc_body = (ticket.extracted or {}).get("guided_confirmation_body")
    if kind == "created" and gc_body:
        gc_subject = (ticket.extracted or {}).get("guided_confirmation_subject") \
            or "Support Ticket Created Successfully"
        care_url = customer_ticket_link(ticket) or _care_panel_tracking_url(ticket)
        body = gc_body + (f"\n\nTrack Ticket:\n{care_url}" if care_url else "")
        logger.info("EMAIL-TEMPLATE-LINK=%s (guided confirmation)",
                    care_url or "(none -- no-link variant)")
        msg = Message.objects.create(
            ticket=ticket, direction=Message.DIRECTION_OUTBOUND,
            from_email=confirm_from,
            to_email=ticket.customer_email, subject=gc_subject, body_text=body,
            sent_at=timezone.now())
        logger.info("M5-SEND ticket=%s kind=%s guided=True from=%s to=%s",
                    ticket.ticket_id, kind, confirm_from, ticket.customer_email)
        sent_id = None
        try:
            sent_id = send_reply(msg)
        except Exception:  # noqa: BLE001 -- confirmation is best-effort
            logger.exception("Guided confirmation send FAILED for %s (from=%s to=%s)",
                             ticket.ticket_id, confirm_from, ticket.customer_email)
        if sent_id:
            logger.info("M5-SENT ticket=%s guided=True provider_id=%s", ticket.ticket_id, sent_id)
        else:
            logger.error("M5-NOT-DELIVERED ticket=%s guided=True from=%s -- send_reply returned "
                         "no id (not configured or sender refused)", ticket.ticket_id, confirm_from)
        AuditLogEntry.objects.create(ticket=ticket, actor="system",
                                     event="confirmation_sent", detail={"kind": kind,
                                     "guided": True, "delivered": bool(sent_id)})
        return msg

    # M5 (created) / M6 (existing) -- in the customer's detected language. The ticket URL is
    # included ONLY when Care Panel ticket CREATION SUCCEEDED, i.e. there is a real Care
    # Panel hash (https://care.deodap.in/t?id=<hash>); otherwise the no-link variant
    # (M5N/M6N) and log why. We never emit an internal/localhost link here.
    number = ticket.ticket_number or ticket.ticket_id
    # A REAL Care Panel number (numeric, from store-json) differs from the internal TKT-... id.
    # When store-json was skipped/failed (e.g. an escalated, unverified ticket with no phone) the
    # number is just the internal id fallback -> we must NOT show it to the customer as a ticket
    # number. We keep the acknowledgment + tracking link but drop the "Ticket ID:" line below.
    has_real_number = bool(ticket.ticket_number) and ticket.ticket_number != ticket.ticket_id
    # Customer-facing View-Ticket link -> OUR /t portal (which shows the full Conversation and
    # resolves ANY hash); fall back to the external Care Panel link only if our portal base is
    # unset. This is why the M5 email now points at care.deodap.info/email_automation/t.
    care_url = customer_ticket_link(ticket) or _care_panel_tracking_url(ticket)
    logger.info("EMAIL-TEMPLATE-LINK=%s", care_url or "(none -- no-link variant)")
    # A verified two-step inquiry (invoice / franchise / dropship / company) gets its OWN
    # category-specific confirmation wording instead of the generic complaint M5.
    verify_kind = (ticket.extracted or {}).get("verify_kind") if kind == "created" else None
    reg_line = _INQUIRY_REGISTERED_LINE.get(verify_kind, {}).get(
        mails.normalize_lang(ticket.language)) if verify_kind else None
    if care_url:
        if reg_line:
            mail_id = "M5_INQUIRY"
            subject, body = mails.render(mail_id, ticket.language, ticket_number=number,
                                         tracking_url=care_url, registered_line=reg_line)
        else:
            mail_id = "M5" if kind == "created" else "M6"
            subject, body = mails.render(mail_id, ticket.language,
                                         ticket_number=number, tracking_url=care_url)
        logger.info("EMAIL_CONTEXT=%s", {"ticket": ticket.ticket_id, "kind": kind,
                    "mail": mail_id, "subject": subject, "ticket_number": number,
                    "tracking_url": care_url, "has_link": True})
    else:
        reason = ("no care_panel_ticket_id (store-json failed / no open-tickets match)"
                  if not (ticket.extracted or {}).get("care_panel_ticket_id")
                  else "hash present but tracking_url empty")
        if reg_line:
            mail_id = "M5_INQUIRY_N"
            subject, body = mails.render(mail_id, ticket.language, ticket_number=number,
                                         registered_line=reg_line)
        else:
            mail_id = "M5N" if kind == "created" else "M6N"
            subject, body = mails.render(mail_id, ticket.language, ticket_number=number)
        logger.info("Confirmation FALLBACK ticket=%s kind=%s reason=%s",
                    ticket.ticket_id, kind, reason)
        logger.info("EMAIL_CONTEXT=%s", {"ticket": ticket.ticket_id, "kind": kind,
                    "mail": mail_id, "subject": subject, "has_link": False, "reason": reason})

    # No real Care Panel number -> never expose the internal TKT-... id: drop the "Ticket ID:" line
    # (all languages) but keep the acknowledgment + tracking link, per the required behavior.
    if not has_real_number:
        body = _suppress_internal_ref(body, number)
        logger.info("CONFIRMATION-ID-SUPPRESSED ticket=%s (no Care Panel number -- internal id "
                    "hidden from the customer).", ticket.ticket_id)

    message = Message.objects.create(
        ticket=ticket, direction=Message.DIRECTION_OUTBOUND,
        from_email=confirm_from,
        to_email=ticket.customer_email, subject=subject,
        body_text=body, sent_at=timezone.now(),
    )
    logger.info("M5-SEND ticket=%s kind=%s mail=%s from=%s to=%s",
                ticket.ticket_id, kind, mail_id, confirm_from, ticket.customer_email)
    sent_id = None
    try:
        sent_id = send_reply(message)
    except Exception:  # noqa: BLE001 -- confirmation is best-effort
        logger.exception("Confirmation send FAILED for %s (mail=%s from=%s to=%s)",
                         ticket.ticket_id, mail_id, confirm_from, ticket.customer_email)
    if sent_id:
        logger.info("M5-SENT ticket=%s mail=%s provider_id=%s", ticket.ticket_id, mail_id, sent_id)
        logger.info("SEND_CONFIRMATION_SUCCESS ticket=%s mail=%s to=%s provider_id=%s",
                    ticket.ticket_id, mail_id, ticket.customer_email, sent_id)
    else:
        logger.error("M5-NOT-DELIVERED ticket=%s mail=%s from=%s -- send_reply returned no id "
                     "(provider not configured or sender refused)", ticket.ticket_id,
                     mail_id, confirm_from)
        logger.error("SEND_CONFIRMATION_FAILED ticket=%s mail=%s to=%s from=%s -- email NOT "
                     "delivered (provider not configured or sender refused).", ticket.ticket_id,
                     mail_id, ticket.customer_email, confirm_from)
    AuditLogEntry.objects.create(
        ticket=ticket, actor="system", event="confirmation_sent",
        detail={"kind": kind, "mail": mail_id, "delivered": bool(sent_id)},
    )
    try:
        from apps.analytics.logging import log_auto_reply
        log_auto_reply(brand=ticket.brand, customer_email=ticket.customer_email, subject=subject,
                       template=("M5" if kind == "created" else "M6"),
                       trigger=f"confirmation_{kind}", ticket=ticket)
    except Exception:  # noqa: BLE001 -- reporting must never break confirmations
        logger.exception("auto-reply logging failed (non-fatal)")
    return message


def process_new_ticket(ticket):
    """A brand-new email: classify -> match existing -> decide.

    Smart Ticket Management:
    - Matches an existing ticket  -> append + update (no duplicate).
    - Needs evidence (no photo/video yet) -> request evidence and DEFER creating the
      Gallabox ticket + "created" confirmation until the evidence arrives.
    - Otherwise -> create ticket + Gallabox + "created" confirmation.
    """
    from apps.decision import policy

    _auto_classify(ticket)
    ticket.refresh_from_db()
    if ticket.is_ignored:  # reports / OTP / newsletters
        return ticket
    # HARD GUARD (Add/Update Items / GST): auto-reply only -- skip the existing-ticket lookup.
    if policy.blocks_ticket(ticket.category, ticket.sub_topic, _ticket_intent_text(ticket)):
        return _handle_no_ticket_subcategory(ticket)

    surviving = match_and_merge(ticket)
    if surviving is not None:
        AuditLogEntry.objects.create(
            ticket=surviving, actor="system", event="ticket_updated",
            detail={"reason": "matched_existing"},
        )
        _auto_decide(surviving)
        _sync_external(surviving)
        send_confirmation(surviving, "updated")
        return surviving

    # New email, no existing ticket matched.
    _auto_decide(ticket)
    ticket.refresh_from_db()

    # Evidence required and not yet provided -> request it, defer ticket creation.
    if ticket.status == Ticket.STATUS_AWAITING_EVIDENCE and not _has_evidence(ticket):
        ticket.pending_evidence = True
        ticket.save(update_fields=["pending_evidence", "updated_at"])
        AuditLogEntry.objects.create(
            ticket=ticket, actor="system", event="evidence_requested",
            detail={"reason": "awaiting_evidence"},
        )
        return ticket  # no Gallabox ticket, no "created" confirmation yet

    # Finalize a genuinely new ticket.
    _sync_external(ticket)
    send_confirmation(ticket, "created")
    return ticket


# === FLOW 1: existing-ticket reply classifier ==================================================
# A reply that threads into an existing ticket is NEVER run through duplicate-ticket detection and
# NEVER receives an 'Existing Ticket Found' (M6) mail. Instead it is sorted into one of four
# intents, so a plain "Thank you" is saved silently, a "status?" gets an automated update, an
# attachment is filed as evidence, and a genuinely new complaint spawns its OWN ticket.
REPLY_STATUS_REQUEST = "STATUS_REQUEST"
REPLY_ADDITIONAL_INFORMATION = "ADDITIONAL_INFORMATION"
REPLY_GENERAL = "GENERAL_REPLY"
REPLY_NEW_ISSUE = "NEW_ISSUE"

_REPLY_STATUS_KW = (
    "any update", "status update", "status?", "the status", "current status", "latest status",
    "any progress", "please update", "kindly update", "update me", "update please", "update?",
    "can you check", "could you check", "please check", "when will", "how long", "resolved yet",
    "still waiting", "update on my", "whats the update", "what's the update", "what is the update",
)
_REPLY_NEW_ISSUE_KW = (
    "another issue", "another problem", "new issue", "new problem", "one more issue",
    "one more problem", "separate issue", "different issue", "different order", "different product",
    "another order", "another product", "payment failed", "refund not received",
    "refund not receive", "not received my refund", "havent received refund",
    "haven't received refund", "have not received refund", "raise a new complaint",
)
_REPLY_ACK_EXACT = {
    "thank you", "thanks", "thank u", "thankyou", "thnx", "thx", "ty", "tq", "done", "ok",
    "okay", "okey", "k", "kk", "received", "noted", "got it", "great", "good", "fine", "sure",
    "perfect", "cool", "alright", "understood", "welcome", "no problem", "appreciate it",
    "thank you so much", "thanks a lot", "thanks alot", "ok thanks", "okay thanks", "nice",
    "yes", "yeah", "yup", "thank you very much",
}
_REPLY_ACK_FIRST = {
    "thank", "thanks", "thankyou", "thx", "ty", "tq", "ok", "okay", "okey", "done", "noted",
    "great", "perfect", "cool", "alright", "appreciate", "welcome", "understood",
}


def _clean_low(message):
    """Lower-cased NEW reply body (quoted thread history stripped) for intent matching. The
    subject is intentionally EXCLUDED: a reply's 'Re: <original>' subject echoes the original
    complaint and would poison keyword detection (e.g. a ticket titled 'Refund not received'
    would make every 'Thank you' reply look like a NEW_ISSUE)."""
    if not message:
        return ""
    body = message.get("body_text") or message.get("snippet") or message.get("body") or ""
    fresh = _clean_reply(body).strip() or body.strip()
    return fresh.lower().strip()


def _classify_reply(ticket, message):
    """Sort a reply on an EXISTING ticket into one of the four reply intents (FLOW 1)."""
    if message is None:
        return REPLY_ADDITIONAL_INFORMATION
    low = _clean_low(message)
    has_attach = _message_has_evidence(message) or bool(
        (message.get("attachment_blobs") or []) or (message.get("attachments") or []))

    # A genuinely different complaint -> route to the normal AI flow (may open a new ticket).
    if any(kw in low for kw in _REPLY_NEW_ISSUE_KW):
        return REPLY_NEW_ISSUE
    # Photo / video / invoice etc. -> evidence / additional information.
    if has_attach:
        return REPLY_ADDITIONAL_INFORMATION
    # "Any update?" / "status?" -> send the automated ticket-status mail.
    if any(kw in low for kw in _REPLY_STATUS_KW):
        return REPLY_STATUS_REQUEST
    # A short acknowledgement with no real content -> save only, no auto-reply.
    words = re.sub(r"[^a-z0-9 ]+", " ", low).split()
    compact = " ".join(words)
    if compact and (compact in _REPLY_ACK_EXACT
                    or (len(words) <= 4 and words[0] in _REPLY_ACK_FIRST)):
        return REPLY_GENERAL
    # Substantive free-text follow-up -> attach it to the ticket + notify the team (never M6).
    return REPLY_ADDITIONAL_INFORMATION


def _latest_customer_facing_note(ticket):
    """The most recent internal/agent note on the ticket (for the status-update mail)."""
    entry = (AuditLogEntry.objects.filter(ticket=ticket, event="internal_note")
             .order_by("-created_at").first())
    note = (entry.detail or {}).get("note") if entry else ""
    return (note or "").strip()


def _send_ticket_update(ticket):
    """STATUS_REQUEST reply -> email the customer the ticket's latest status, note and link.
    Never creates a ticket and never sends 'Existing Ticket Found'."""
    if ticket.is_ignored or not ticket.customer_email:
        return None
    number = ticket.ticket_number or ticket.ticket_id
    try:
        status_txt = ticket.get_status_display()
    except Exception:  # noqa: BLE001 -- status may lack choices
        status_txt = (ticket.status or "").replace("_", " ").title()
    latest_note = _latest_customer_facing_note(ticket) \
        or "Your ticket is being reviewed by our team and we will update you shortly."
    link = customer_ticket_link(ticket) or _care_panel_tracking_url(ticket) or ""
    sign = mails.SIGN.get(mails.normalize_lang(ticket.language), mails.SIGN[mails.DEFAULT_LANG])
    subject = f"Ticket Update - {number}"
    lines = ["Dear Customer,", "", "Here is the latest update for your ticket.", "",
             "Ticket ID:", str(number), "", "Current Status:", str(status_txt), "",
             "Latest Update:", str(latest_note)]
    if link:
        lines += ["", "View Ticket:", link]
    body = "\n".join(lines) + f"\n\n{sign}"
    confirm_from = reply_from_address(ticket.mailbox)
    msg = Message.objects.create(
        ticket=ticket, direction=Message.DIRECTION_OUTBOUND, from_email=confirm_from,
        to_email=ticket.customer_email, subject=subject, body_text=body, sent_at=timezone.now())
    logger.info("STATUS-UPDATE-SEND ticket=%s to=%s status=%s", ticket.ticket_id,
                ticket.customer_email, ticket.status)
    try:
        send_reply(msg)
    except Exception:  # noqa: BLE001 -- best-effort
        logger.exception("Ticket-update reply send FAILED for %s", ticket.ticket_id)
    AuditLogEntry.objects.create(ticket=ticket, actor="system", event="status_update_sent",
                                 detail={"status": ticket.status})
    return msg


def _base_category(cat):
    """Leading category code ('8. Payment & Invoice' -> '8')."""
    return (cat or "").split(".")[0].strip().lower()


def _handle_reply_new_issue(mailbox, parent, message):
    """A reply raising a genuinely DIFFERENT complaint. Run the normal AI classification; if the
    issue differs from the parent ticket, open a NEW ticket in its own thread via the standard
    new-email flow (which includes the duplicate check). Same issue -> just attach the info."""
    result = _classify_dict(mailbox.brand, message)
    parent_cat = _base_category(parent.category)
    new_cat = _base_category(getattr(result, "category", "") if result else "")
    is_support = bool(result and getattr(result, "is_support_request", True))
    if not result or not is_support or (new_cat and new_cat == parent_cat):
        # Not a distinct new issue -> attach to the existing ticket (never 'Existing Ticket Found').
        _add_internal_note(parent, "Additional information received from customer")
        _sync_external(parent)
        _upload_care_panel_media(parent)
        return parent
    # Distinct new issue -> spin up a SEPARATE ticket in its own thread and run the full flow.
    new_thread = (message.get("message_id") or message.get("gmail_message_id")
                  or f"{parent.thread_id or parent.ticket_id}:newissue")
    fork = dict(message)
    fork["thread_id"] = new_thread
    fork["message_id"] = new_thread
    fork["gmail_message_id"] = f"{message.get('gmail_message_id') or new_thread}::newissue"
    fork["in_reply_to"] = ""
    fork["references"] = []
    ticket, _msg, created = ingest_message(mailbox, fork)
    _add_internal_note(parent, f"Customer raised a separate issue -> new ticket {ticket.ticket_id}")
    if created and getattr(ticket, "_created_now", False) and not ticket.is_ignored:
        _add_internal_note(ticket, f"Split from a reply on ticket {parent.ticket_id}")
        process_new_ticket(ticket)
    logger.info("REPLY_NEW_ISSUE parent=%s new_ticket=%s parent_cat=%s new_cat=%s",
                parent.ticket_id, ticket.ticket_id, parent_cat or "-", new_cat or "-")
    return ticket


def process_existing_reply(ticket, message=None, mailbox=None):
    """A reply / follow-up (incl. photo/video) on an EXISTING ticket (FLOW 1). The message +
    attachments were already appended at ingest. A reply is NEVER run through duplicate-ticket
    detection and NEVER receives an 'Existing Ticket Found' mail; it is routed by a lightweight
    reply classifier instead:
        STATUS_REQUEST         -> email the latest status/note/link (no ticket, no M6)
        ADDITIONAL_INFORMATION -> attach + notify the team (and finalize a deferred ticket once
                                  its evidence arrives)
        GENERAL_REPLY          -> save only; no auto-reply
        NEW_ISSUE              -> run the normal AI flow (may open a new, separate ticket)
    `message`/`mailbox` are optional for backward compatibility (message=None behaves as
    ADDITIONAL_INFORMATION)."""
    ticket.refresh_from_db()
    if ticket.is_ignored:
        return ticket

    was_pending = ticket.pending_evidence
    has_evidence = _has_evidence(ticket)

    # (A) A still-deferred ticket whose evidence has NOW arrived -> finalize it (create the
    #     external ticket + send the 'created' confirmation). Unchanged evidence-completion path.
    if was_pending:
        _auto_decide(ticket)   # re-evaluate: evidence now present moves it forward
        ticket.refresh_from_db()
        if not has_evidence:
            return ticket      # still waiting; the engine re-requested evidence
        # Evidence arrived -> NOW create the Gallabox ticket + "created" confirmation.
        ticket.pending_evidence = False
        ticket.save(update_fields=["pending_evidence", "updated_at"])
        _add_internal_note(ticket, "Additional evidence received from customer")
        _sync_external(ticket)
        _upload_care_panel_media(ticket)
        send_confirmation(ticket, "created")
        return ticket

    # (B) Established ticket -> classify the reply. This REPLACES the old blanket
    #     send_confirmation(ticket, "updated") that wrongly emailed 'Existing Ticket Found' for
    #     every follow-up (incl. a plain 'Thank you').
    reply_type = _classify_reply(ticket, message)
    logger.info("REPLY_CLASSIFIED ticket=%s type=%s from=%s", ticket.ticket_id, reply_type,
                (message or {}).get("from_email") or "-")

    if reply_type == REPLY_NEW_ISSUE and mailbox is not None and message is not None:
        return _handle_reply_new_issue(mailbox, ticket, message)

    if reply_type == REPLY_STATUS_REQUEST:
        _send_ticket_update(ticket)
        return ticket

    if reply_type == REPLY_ADDITIONAL_INFORMATION:
        _add_internal_note(ticket, "Additional evidence received from customer" if has_evidence
                           else "Additional information received from customer")
        _sync_external(ticket)
        _upload_care_panel_media(ticket)
        return ticket

    # GENERAL_REPLY (e.g. "Thank you") -> the message is already saved; do nothing else.
    return ticket


def _sync_gallabox(ticket):
    """Mirror the ticket to Gallabox if configured. Best-effort; never blocks."""
    try:
        from apps.integrations import gallabox

        ticket.refresh_from_db()
        gallabox.sync_ticket(ticket)
    except Exception:  # noqa: BLE001
        logger.exception("Gallabox sync hook failed for %s", ticket.ticket_id)


def _verify_awb(ticket):
    """Verify an AI-extracted AWB against ship.deodap.com before it's used (§5/§9)."""
    try:
        from apps.integrations import shipping

        ticket.refresh_from_db()
        shipping.annotate_awb_verification(ticket)
    except Exception:  # noqa: BLE001 -- verification is best-effort
        logger.exception("AWB verify hook failed for %s", ticket.ticket_id)


def _sync_external(ticket):
    """Mirror the ticket to the external systems (Gallabox + DeoDap Care Panel API).
    Each is find-or-create by customer email + order id; best-effort."""
    _verify_awb(ticket)
    _sync_gallabox(ticket)
    try:
        from apps.integrations import care_panel

        ticket.refresh_from_db()
        care_panel.sync_ticket(ticket)
    except Exception:  # noqa: BLE001
        logger.exception("Care Panel sync hook failed for %s", ticket.ticket_id)


def _store_care_panel(ticket):
    """Create the ticket in the DeoDap Care Panel (store-json) and save the customer
    tracking link + ticket number. Best-effort. No-op if the store API isn't configured.

    Stores once per REAL Care Panel ticket: skip only when a genuine Care Panel hash
    already exists. A ticket whose tracking_url is merely our INTERNAL fallback (the
    store-json transiently failed before) is re-attempted so it can still get a real
    care.deodap.in link on a later pass."""
    from apps.decision import policy

    # MANDATORY SAFETY CHECK: Add / Update Items and Add / Update GST Details (Make Changes To
    # Order) must NEVER create a ticket -- auto-reply only. Block before create_ticket().
    if policy.blocks_ticket(ticket.category, ticket.sub_topic, _ticket_intent_text(ticket)):
        logger.warning("Blocked ticket creation for %s (category=%s) -- auto-reply only, NO "
                       "Care Panel ticket.", ticket.sub_topic or "-", ticket.category or "-")
        return
    extracted = ticket.extracted or {}
    if _care_panel_tracking_url(ticket):   # already has a REAL Care Panel hash
        return
    if ticket.tracking_url and not extracted.get("internal_tracking") \
            and not _is_internal_tracking_url(ticket.tracking_url):
        return                                  # a real external link already set

    # Some flows (e.g. the delivered-item EVIDENCE flow) create a ticket from an order_id / typed
    # phone WITHOUT verifying it against Shopify, so the VERIFIED customer name/phone/email were
    # never stamped -> the Care Panel shows "Unknown" (and store-json may lack a phone -> no link).
    # Resolve the ORDER OWNER here whenever we are missing the phone OR the verified name, using
    # any identifier we have (order_id / typed phone / email). Order owner ALWAYS wins.
    needs_phone = not extracted.get("phone")
    needs_name = extracted.get("customer_name_source") != "shopify_verified"
    has_identifier = extracted.get("order_id") or extracted.get("phone") or extracted.get("email")
    if (needs_phone or needs_name) and has_identifier:
        try:
            status, info = _shopify_verify(
                ticket.brand, extracted.get("order_id") or "", extracted.get("phone") or "",
                extracted.get("email") or "", workflow="care_panel_store")
            if status == "verified":
                ticket.extracted = _stamp_verified_customer(dict(extracted), info)
                ticket.save(update_fields=["extracted", "updated_at"])
                extracted = ticket.extracted
                logger.info("CARE_PANEL_OWNER_RESOLVED order=%s phone=%s name=%s (verified owner "
                            "stamped for the Care Panel ticket)", extracted.get("order_id"),
                            extracted.get("phone"), extracted.get("customer_name"))
            else:
                logger.warning("CARE_PANEL_OWNER_UNRESOLVED order=%s phone=%s status=%s -- name "
                               "stays 'Unknown'.", extracted.get("order_id"),
                               extracted.get("phone"), status)
        except Exception:  # noqa: BLE001 -- best-effort; never block the store attempt
            logger.exception("Care Panel owner-resolution failed for %s", ticket.ticket_id)
    try:
        from apps.integrations import care_panel_store

        ticket.refresh_from_db()
        care_panel_store.store_ticket(ticket)
        ticket.refresh_from_db()
    except Exception:  # noqa: BLE001
        logger.exception("Care Panel store hook failed for %s", ticket.ticket_id)


def _upload_care_panel_media(ticket):
    """Push the ticket's photo/video attachments to its Care Panel tracking page ('Media Files')
    AND sync the email conversation into the ticket thread. Needs a matched care_panel_ticket_id.
    Both are best-effort + idempotent (media dedup by sha256, conversation dedup by message id)."""
    try:
        from apps.integrations import care_panel_media

        ticket.refresh_from_db()
        care_panel_media.upload_attachments(ticket)
    except Exception:  # noqa: BLE001
        logger.exception("Care Panel media hook failed for %s", ticket.ticket_id)
    try:
        from apps.integrations import care_panel_media

        care_panel_media.sync_conversation(ticket)
    except Exception:  # noqa: BLE001 -- the thread sync must never block the pipeline
        logger.exception("Care Panel conversation hook failed for %s", ticket.ticket_id)


def _auto_decide(ticket):
    """Run the decision engine on a freshly classified ticket (doc pipeline:
    AI classifier -> Decision engine). Best-effort: never blocks ingestion."""
    try:
        from apps.decision import engine
        from apps.integrations import context as live_context

        ticket.refresh_from_db()
        # Scan the conversation's stored attachments FIRST so the engine's evidence
        # check sees photos/videos already received and never re-asks for them.
        _sync_evidence_flags(ticket)
        ticket.refresh_from_db()
        facts = live_context.build_context(ticket)
        # Always give templates a usable tracking link: prefer the live link (facts /
        # extracted / ticket field); only when none exists do we fall back to our
        # internal /t page -- so order-status replies never leave {tracking_url} unfilled.
        have_url = (facts.get("tracking_url") or (ticket.extracted or {}).get("tracking_url")
                    or ticket.tracking_url)
        if not have_url:
            facts["tracking_url"] = _context_tracking_url(ticket)
        engine.run(ticket, context=facts)
    except Exception:  # noqa: BLE001 -- decisioning is best-effort here
        logger.exception("Auto-decide failed for ticket %s", ticket.ticket_id)


def _reply_threading_headers(ticket):
    """In-Reply-To / References from the ticket's latest inbound mail, so the reply
    threads in the customer's mailbox."""
    last_inbound = (
        ticket.messages.filter(direction=Message.DIRECTION_INBOUND)
        .order_by("-created_at")
        .first()
    )
    in_reply_to = ""
    references = []
    if last_inbound:
        headers = last_inbound.headers or {}
        in_reply_to = headers.get("Message-ID") or headers.get("Message-Id", "")
        references = list(last_inbound.references or [])
        if in_reply_to and in_reply_to not in references:
            references.append(in_reply_to)
    return in_reply_to, references


def _record_sent(message, sent_id, set_sent_at=False):
    """Store the provider's message id, tolerating a duplicate id without breaking
    the surrounding transaction (the unique gmail_message_id is best-effort here)."""
    message.gmail_message_id = sent_id
    fields = ["gmail_message_id", "updated_at"]
    if set_sent_at:
        message.sent_at = message.sent_at or timezone.now()
        fields.append("sent_at")
    try:
        with transaction.atomic():
            message.save(update_fields=fields)
    except IntegrityError:
        message.gmail_message_id = None
        if set_sent_at:
            message.save(update_fields=["sent_at", "updated_at"])


# A customer email must NEVER contain a raw template placeholder. Any {word} that
# survived rendering (e.g. {tracking_url}/{edd} with no live data) triggers a safe
# fallback instead of leaking the literal placeholder.
_UNRESOLVED_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z0-9_]+\}")
SAFE_FALLBACK_BODY = (
    "Hello,\n\nThank you for contacting DeoDap. We have received your request and our "
    "team is reviewing it. We'll update you shortly.\n\nRegards,\nDeoDap Support Team"
)


def _guard_unresolved_placeholders(message):
    """Last line of defense before an email leaves: if the body still has an unresolved
    {placeholder}, log TEMPLATE_RENDER_ERROR and swap in a safe fallback so the customer
    never sees raw template syntax. Returns True if it had to fall back."""
    leftovers = _UNRESOLVED_PLACEHOLDER_RE.findall(message.body_text or "")
    if not leftovers:
        return False
    tid = message.ticket.ticket_id if message.ticket_id else "?"
    logger.error("TEMPLATE_RENDER_ERROR ticket=%s message=%s unresolved=%s -> safe fallback",
                 tid, message.id, leftovers)
    message.body_text = SAFE_FALLBACK_BODY
    message.save(update_fields=["body_text"])
    if message.ticket_id:
        AuditLogEntry.objects.create(
            ticket=message.ticket, actor="system", event="template_render_error",
            detail={"unresolved": leftovers, "message_id": message.id},
        )
    return True


def send_reply(message, client=None):
    """Send an outbound Message to the customer. Uses SMTP when EMAIL_PROVIDER=imap,
    otherwise the Gmail API. Returns the sent message id, or None if not configured
    (the caller keeps the local record either way)."""
    from django.conf import settings

    ticket = message.ticket
    # NEVER send unresolved {placeholders} to a customer (safe fallback if any remain).
    _guard_unresolved_placeholders(message)
    to = message.to_email or ticket.customer_email
    subject = message.subject or f"Re: {ticket.subject}"
    in_reply_to, references = _reply_threading_headers(ticket)

    # --- IMAP provider -> send via SMTP ---
    if getattr(settings, "EMAIL_PROVIDER", "imap") == "imap":
        from .smtp_client import send_email

        # Send FROM the agent's chosen sender (message.from_email, validated to an active
        # SupportEmail / 'send as' alias by the viewset), else REPLY_FROM / IMAP_USER. Reply-To is
        # the fetched inbox so the customer's reply returns to the address we poll.
        from_addr = (message.from_email or "").strip() \
            or getattr(settings, "REPLY_FROM", "") or settings.IMAP_USER or None
        sent_id = send_email(
            to=to, subject=subject, body_text=message.body_text,
            from_addr=from_addr, reply_to=primary_inbox_address(ticket.mailbox),
            in_reply_to=in_reply_to, references=references,
        )
        if sent_id:
            _record_sent(message, sent_id, set_sent_at=True)
        return sent_id

    # --- Gmail API provider ---
    mailbox = ticket.mailbox
    if mailbox is None:
        return None
    client = client or build_client(mailbox)
    if client is None:
        return None
    sent_id = client.send_message(
        thread_id=ticket.thread_id, to=to, subject=subject,
        body_text=message.body_text, in_reply_to=in_reply_to, references=references,
    )
    if sent_id:
        _record_sent(message, sent_id)
    return sent_id
