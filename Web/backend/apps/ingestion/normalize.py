"""
Normalize a raw Gmail API message resource into a flat dict the rest of the
engine understands (doc section 2, "Per-mail flow" steps 3-4).

This is pure parsing -- no Django, no Gmail client, no network -- so it is fully
unit-testable by feeding it a `users.messages.get` (format=full) response.

Output shape (the "normalized message"):
    {
        "gmail_message_id": "<Gmail internal id, used for dedup>",
        "thread_id":        "<Gmail threadId, used for threading>",
        "message_id":       "<RFC822 Message-ID header>",
        "in_reply_to":      "<In-Reply-To header>",
        "references":       ["<msg-id>", ...],
        "from_email":       "buyer@example.com",
        "from_name":        "Buyer Name",
        "to":               "care@deodap.com",
        "subject":          "Order not received",
        "body_text":        "...",          # prefer text/plain
        "body_html":        "...",          # fallback / original
        "headers":          {"From": "...", "Precedence": "bulk", ...},
        "attachments":      [{"filename": "x.pdf", "mime_type": "...",
                              "attachment_id": "...", "size": 1234}],
        "label_ids":        ["INBOX", ...],
        "snippet":          "...",
    }
"""

import base64
from email.utils import getaddresses, parseaddr


def _b64url_decode(data):
    """Gmail body payloads are base64url with padding stripped."""
    if not data:
        return ""
    padding = "-_"  # base64url alphabet markers; just ensure correct padding
    s = data.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return ""


def _headers_to_dict(header_list):
    """Gmail returns headers as [{'name': ..., 'value': ...}]. Last wins."""
    out = {}
    for h in header_list or []:
        name = h.get("name", "")
        if name:
            out[name] = h.get("value", "")
    return out


def _header(headers, name, default=""):
    """Case-insensitive header lookup over the {name: value} dict."""
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return default


def _walk_parts(payload, text_acc, html_acc, attachments):
    """Recursively collect text/plain, text/html and attachment metadata."""
    if not payload:
        return
    mime = payload.get("mimeType", "")
    body = payload.get("body", {}) or {}
    filename = payload.get("filename", "")

    if filename:
        # An attachment part: data lives behind an attachmentId (fetched lazily).
        attachments.append(
            {
                "filename": filename,
                "mime_type": mime,
                "attachment_id": body.get("attachmentId", ""),
                "size": body.get("size", 0),
            }
        )
    elif mime == "text/plain":
        text_acc.append(_b64url_decode(body.get("data", "")))
    elif mime == "text/html":
        html_acc.append(_b64url_decode(body.get("data", "")))

    for part in payload.get("parts", []) or []:
        _walk_parts(part, text_acc, html_acc, attachments)


def _split_references(value):
    """References / In-Reply-To headers are whitespace-separated angle-bracketed ids."""
    if not value:
        return []
    return [tok for tok in value.replace("\n", " ").split() if tok.strip()]


def parse_gmail_message(raw):
    """Convert a Gmail `users.messages.get` (format=full) resource to a normalized dict."""
    payload = raw.get("payload", {}) or {}
    headers = _headers_to_dict(payload.get("headers", []))

    from_name, from_email = parseaddr(_header(headers, "From"))
    to_value = _header(headers, "To")
    to_emails = [addr for _, addr in getaddresses([to_value])] if to_value else []
    cc_value = _header(headers, "Cc")
    cc_emails = [addr for _, addr in getaddresses([cc_value])] if cc_value else []
    bcc_value = _header(headers, "Bcc")
    bcc_emails = [addr for _, addr in getaddresses([bcc_value])] if bcc_value else []

    text_parts, html_parts, attachments = [], [], []
    _walk_parts(payload, text_parts, html_parts, attachments)

    body_text = "\n".join(p for p in text_parts if p).strip()
    body_html = "\n".join(p for p in html_parts if p).strip()
    # If only HTML arrived, keep it; the agent UI can render it. text stays empty
    # rather than us shipping a half-baked HTML->text stripper here (doc: "prefer
    # text/plain, fall back to sanitized HTML").

    return {
        "gmail_message_id": raw.get("id", ""),
        "thread_id": raw.get("threadId", ""),
        "message_id": _header(headers, "Message-ID") or _header(headers, "Message-Id"),
        "in_reply_to": _header(headers, "In-Reply-To"),
        "references": _split_references(_header(headers, "References")),
        "from_email": from_email.lower(),
        "from_name": from_name,
        "to": ", ".join(to_emails) or to_value,
        "cc": ", ".join(cc_emails),
        "bcc": ", ".join(bcc_emails),
        "subject": _header(headers, "Subject"),
        "body_text": body_text,
        "body_html": body_html,
        "headers": headers,
        "attachments": attachments,
        "label_ids": raw.get("labelIds", []),
        "snippet": raw.get("snippet", ""),
    }
