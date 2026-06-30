"""
Send replies over SMTP (the IMAP provider's outbound side). Reuses the IMAP
account credentials and derives the SMTP host from the IMAP host (imap.zoho.in ->
smtp.zoho.in) unless SMTP_HOST is set explicitly.

`send_email` is the only entry point; it threads the reply by setting In-Reply-To /
References so it lands in the customer's existing conversation.
"""

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from django.conf import settings

logger = logging.getLogger(__name__)


def is_configured():
    return bool(settings.SMTP_HOST and settings.IMAP_USER and settings.IMAP_PASSWORD)


def _from_header(sender):
    """A friendly From with a display name -- a bare address from a programmatic sender is a
    spam signal. Keeps the EMAIL the authenticated account (Gmail rejects/junks a mismatched
    From); only adds a name. Honors a name already present in `sender`."""
    name, addr = parseaddr(sender)
    addr = addr or sender
    name = name or getattr(settings, "REPLY_FROM_NAME", "") or "DeoDap Support"
    return formataddr((name, addr)), addr


def send_email(*, to, subject, body_text, from_addr=None, in_reply_to="", references=None,
               body_html=None, attachments=None, reply_to=None):
    """Send an email via SMTP. Plain-text always; when `body_html` is given the message is
    multipart/alternative so the customer's client renders the HTML (e.g. a 'Track Order'
    hyperlink instead of a raw URL) and falls back to the text part. `attachments` is an
    optional list of (filename, content_bytes, content_type) -- e.g. the company brochure PDF.
    `reply_to` overrides the Reply-To header -- set it to the ONE fetched inbox so customer
    replies always come back to the address we poll, even when From is a 'send as' alias.
    Returns the Message-ID on success, raises on any SMTP failure."""
    sender = from_addr or settings.IMAP_USER
    refs = " ".join(references or ([in_reply_to] if in_reply_to else []))
    if not is_configured():
        # Loud + specific -- this silently no-op'd before, so replies vanished with no trace.
        logger.error("SMTP-SEND-FAILED reason=not_configured smtp_host=%r imap_user=%r "
                     "has_password=%s to=%r subject=%r -> reply NOT sent.",
                     settings.SMTP_HOST, settings.IMAP_USER, bool(settings.IMAP_PASSWORD),
                     to, subject)
        raise RuntimeError("SMTP not configured (SMTP_HOST / IMAP_USER / IMAP_PASSWORD missing).")

    from_header, from_email = _from_header(sender)
    # Message-ID domain MUST align with the sending domain (gmail.com), not the server's
    # internal hostname -- a mismatched/non-routable Message-ID domain is a classic spam
    # signal. make_msgid() defaults to socket.getfqdn() (e.g. an internal box name).
    domain = from_email.rsplit("@", 1)[-1] or "localhost"

    msg = EmailMessage()
    message_id = make_msgid(domain=domain)
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=True)
    msg["From"] = from_header
    msg["To"] = to
    # Reply-To = the fetched inbox (when given) so replies to a 'send as' ALIAS still come back to
    # the ONE inbox we poll; otherwise fall back to the From address.
    msg["Reply-To"] = (reply_to or "").strip() or from_email
    msg["Subject"] = subject
    # Mark as a system auto-reply so receivers classify it as transactional, not bulk, and
    # don't auto-reply back (loop prevention).
    msg["Auto-Submitted"] = "auto-replied"
    msg["X-Auto-Response-Suppress"] = "All"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(references or [in_reply_to])
    elif references:
        msg["References"] = " ".join(references)
    msg.set_content(body_text or "")
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    for fname, content, ctype in (attachments or []):
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(content, maintype=maintype, subtype=subtype or "octet-stream",
                           filename=fname)

    host, port = settings.SMTP_HOST, settings.SMTP_PORT
    # Timeout must cover UPLOADING attachments -- 15s dropped mid-transfer on multi-MB files
    # ('SMTPServerDisconnected'). Scale with the message size (~1s/100KB) on top of a 60s floor.
    msg_bytes = len(msg.as_bytes())
    timeout = max(60, int(getattr(settings, "SMTP_TIMEOUT", 60)), msg_bytes // 100_000)
    logger.info("SMTP-SEND-START host=%s port=%s ssl=%s from=%r to=%r subject=%r "
                "message_id=%s size=%d timeout=%ds in_reply_to=%s references=%s",
                host, port, settings.SMTP_USE_SSL, sender, to, subject, message_id,
                msg_bytes, timeout, in_reply_to or "-", refs or "-")
    try:
        if settings.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            server.starttls()
    except Exception as exc:  # noqa: BLE001
        logger.error("SMTP-SEND-FAILED stage=connect host=%s port=%s ssl=%s to=%r error=%r",
                     host, port, settings.SMTP_USE_SSL, to, exc)
        raise
    try:
        login_code, login_resp = server.login(settings.IMAP_USER, settings.IMAP_PASSWORD)
        logger.info("SMTP-RESPONSE stage=login code=%s resp=%r", login_code, login_resp)
        # send_message returns a dict of recipients the server REFUSED (empty = all accepted);
        # a single refused recipient instead raises SMTPRecipientsRefused. Either way we catch
        # it, so a "sent" that the server actually rejected can no longer pass silently.
        refused = server.send_message(msg)
        if refused:
            logger.error("SMTP-SEND-FAILED stage=recipients_refused to=%r refused=%r",
                         to, refused)
            raise smtplib.SMTPRecipientsRefused(refused)
        logger.info("SMTP-RESPONSE stage=data code=250 (accepted by %s)", host)
        logger.info("SMTP-SEND-SUCCESS to=%r message_id=%s", to, message_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("SMTP-SEND-FAILED stage=send host=%s from=%r to=%r subject=%r error=%r",
                     host, sender, to, subject, exc)
        raise
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass
    return message_id
