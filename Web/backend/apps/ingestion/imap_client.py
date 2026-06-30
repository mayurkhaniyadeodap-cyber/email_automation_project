"""
IMAP email ingestion -- the simplest way to pull mail (no OAuth / Google Cloud).

Connects to any IMAP mailbox (Zoho / Gmail / Outlook) with host + user + password
(use an APP PASSWORD for Gmail/Zoho), fetches recent messages, and parses each
RFC822 message into the same normalized dict the rest of the engine consumes.

`parse_rfc822` is pure (no network), so it's unit-testable from a raw .eml byte
string; the network part (`ImapClient.fetch_recent`) is thin and injectable.
"""

import email
import imaplib
import logging
import re
from email import policy
from email.utils import getaddresses, parseaddr

from django.conf import settings

logger = logging.getLogger(__name__)


def _split_refs(value):
    if not value:
        return []
    return [t for t in value.replace("\n", " ").split() if t.strip()]


def parse_rfc822(raw_bytes):
    """Parse a raw RFC822 message into the engine's normalized message dict."""
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)

    from_name, from_email = parseaddr(msg.get("From", ""))
    to_emails = [addr for _, addr in getaddresses([msg.get("To", "")])]
    cc_emails = [addr for _, addr in getaddresses([msg.get("Cc", "")])]
    bcc_emails = [addr for _, addr in getaddresses([msg.get("Bcc", "")])]

    headers = {k: v for k, v in msg.items()}

    body_text, body_html = "", ""
    attachments = []
    blobs = []  # (filename, mime_type, raw bytes) -- kept out of the JSON metadata
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = (part.get_content_disposition() or "")
            ctype = part.get_content_type()
            filename = part.get_filename()
            if disp == "attachment" or filename:
                payload = part.get_payload(decode=True) or b""
                name = filename or "attachment"
                attachments.append({
                    "filename": name, "mime_type": ctype, "size": len(payload),
                })
                blobs.append({"filename": name, "mime_type": ctype, "content": payload})
            elif ctype == "text/plain" and not body_text:
                body_text = part.get_content()
            elif ctype == "text/html" and not body_html:
                body_html = part.get_content()
    else:
        if msg.get_content_type() == "text/html":
            body_html = msg.get_content()
        else:
            body_text = msg.get_content()

    return {
        "message_id": msg.get("Message-ID", "") or "",
        "thread_id": "",  # resolved by the fetch service via References/In-Reply-To
        "in_reply_to": msg.get("In-Reply-To", "") or "",
        "references": _split_refs(msg.get("References", "")),
        "from_email": (from_email or "").lower(),
        "from_name": from_name,
        "to": ", ".join(to_emails) or msg.get("To", ""),
        "cc": ", ".join(cc_emails),
        "bcc": ", ".join(bcc_emails),
        "subject": msg.get("Subject", "") or "",
        "body_text": (body_text or "").strip(),
        "body_html": (body_html or "").strip(),
        "headers": headers,
        "attachments": attachments,
        "attachment_blobs": blobs,
    }


class ImapClient:
    def __init__(self, host, port, user, password, use_ssl=True, folder="INBOX"):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.use_ssl = use_ssl
        self.folder = folder

    @classmethod
    def from_settings(cls):
        if not settings.IMAP_HOST or not settings.IMAP_USER:
            return None
        return cls(
            settings.IMAP_HOST, settings.IMAP_PORT, settings.IMAP_USER,
            settings.IMAP_PASSWORD, settings.IMAP_USE_SSL, settings.IMAP_FOLDER,
        )

    def _connect(self):
        conn = (
            imaplib.IMAP4_SSL(self.host, self.port) if self.use_ssl
            else imaplib.IMAP4(self.host, self.port)
        )
        conn.login(self.user, self.password)
        return conn

    def _uidvalidity(self, conn):
        typ, resp = conn.status(self.folder, "(UIDVALIDITY)")
        if typ == "OK" and resp and resp[0]:
            m = re.search(rb"UIDVALIDITY (\d+)", resp[0])
            if m:
                return int(m.group(1))
        return None

    def fetch_new(self, last_uid=0, uidvalidity=None, limit=None):
        """Fetch only mail with UID > last_uid (UID-based incremental fetch).

        Returns (current_uidvalidity, [(uid, normalized_dict), ...]) sorted by UID.
        On the FIRST run (last_uid == 0) only UNSEEN mail is pulled, so an existing
        mailbox isn't replayed in full. If UIDVALIDITY changed (mailbox reset), we
        start over (Message-ID dedup still prevents duplicate tickets).
        """
        last_uid = int(last_uid or 0)
        limit = limit or settings.IMAP_FETCH_LIMIT
        conn = self._connect()
        try:
            conn.select(self.folder)
            current_validity = self._uidvalidity(conn)
            if uidvalidity and current_validity and current_validity != uidvalidity:
                last_uid = 0  # mailbox reset -> re-evaluate from scratch

            if last_uid:
                typ, data = conn.uid("search", None, f"(UID {last_uid + 1}:*)")
            else:
                typ, data = conn.uid("search", None, "UNSEEN")
            raw_uids = data[0].split() if data and data[0] else []
            # IMAP returns the highest UID for "N:*" even if < N -> filter strictly.
            uids = sorted({int(u) for u in raw_uids if int(u) > last_uid})[-limit:]

            out = []
            for uid in uids:
                typ, msg_data = conn.uid("fetch", str(uid), "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                out.append((uid, parse_rfc822(msg_data[0][1])))
            return current_validity, out
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    def fetch_recent(self, limit=None, unseen_only=False):
        """Most recent messages (used by the smoke test). Prefer fetch_new()."""
        _, items = self.fetch_new(last_uid=0, limit=limit)
        return [msg for _uid, msg in items]
