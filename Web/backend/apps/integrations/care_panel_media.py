"""
Upload customer photo/video attachments to the DeoDap Care Panel so they show under
"Media Files" on the public tracking page (care.deodap.in/t?id=...).

Mechanism (discovered + verified live against the tracking page's comment form):

    GET  https://care.deodap.in/t?id=<hashId>     -> CSRF _token + laravel_session
    POST https://care.deodap.in/t/add_comment     (multipart/form-data)
         _token=<csrf>  hashId=<hash>  comment=<text>  attachments[]=<file>...

The hashId is the open-tickets `id` (the t?id= value), stored on the ticket as
extracted["care_panel_ticket_id"]. Accepted types: image/jpeg, image/png, video/mp4,
video/quicktime, application/pdf.
"""

import logging
import re

logger = logging.getLogger(__name__)

TRACKING_BASE = "https://care.deodap.in"
# The Care Panel comment form only accepts these (rejects e.g. webp/gif with 422).
ACCEPTED_TYPES = {"image/jpeg", "image/jpg", "image/png", "video/mp4",
                  "video/quicktime", "application/pdf"}
_TOKEN_RE = re.compile(r'name="_token"\s+value="([^"]+)"')
DEFAULT_COMMENT = "Customer shared photo / video evidence."


def _is_uploadable(att):
    ct = (att.content_type or "").lower()
    return ct.startswith("image/") or ct.startswith("video/") or ct == "application/pdf"


def _prepare_file(att):
    """Return (filename, bytes, content_type) ready for the Care Panel, converting
    unsupported images (webp/gif/…) to PNG. Returns None for unsupported videos."""
    ct = (att.content_type or "").lower()
    try:
        with att.file.open("rb") as fh:
            data = fh.read()
    except Exception:  # noqa: BLE001
        logger.exception("Could not read attachment %s", att.id)
        return None

    if ct in ACCEPTED_TYPES:
        return att.filename, data, att.content_type
    if ct.startswith("image/"):
        # Convert webp/gif/etc -> PNG so the photo still shows.
        try:
            import io
            from PIL import Image

            img = Image.open(io.BytesIO(data)).convert("RGBA")
            out = io.BytesIO()
            img.save(out, format="PNG")
            name = re.sub(r"\.[^.]+$", "", att.filename) + ".png"
            logger.info("Care Panel media: converted %s (%s) -> PNG", att.filename, ct)
            return name, out.getvalue(), "image/png"
        except Exception:  # noqa: BLE001
            logger.exception("Could not convert image %s (%s) to PNG", att.filename, ct)
            return None
    logger.info("Care Panel media: skipping unsupported file %s (%s)", att.filename, ct)
    return None


def upload_attachments(ticket, comment=None, session=None):
    """Push the ticket's not-yet-uploaded media to its Care Panel tracking page.

    Returns the number of files uploaded. Best-effort + fully logged. Marks uploaded
    attachments by stamping `remote_url` so they aren't re-sent.
    """
    from apps.tickets.models import AuditLogEntry

    import hashlib

    hash_id = (ticket.extracted or {}).get("care_panel_ticket_id")
    if not hash_id:
        logger.info("Care Panel media SKIP %s: no care_panel_ticket_id (hashId) yet.",
                    ticket.ticket_id)
        return 0

    # Hashes already uploaded for THIS ticket -> never upload the same bytes twice.
    uploaded_hashes = set(
        ticket.attachments.exclude(remote_url="").exclude(sha256="")
        .values_list("sha256", flat=True)
    )

    pending, seen = [], set(uploaded_hashes)
    for a in ticket.attachments.filter(remote_url=""):
        if not _is_uploadable(a):
            continue
        digest = a.sha256
        if not digest:                      # backfill hash for older rows
            try:
                with a.file.open("rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
                a.sha256 = digest
                a.save(update_fields=["sha256", "updated_at"])
            except Exception:  # noqa: BLE001
                logger.exception("Could not hash attachment %s", a.id)
                continue
        if digest in seen:                  # duplicate -> mark uploaded, do NOT re-send
            a.remote_url = ticket.tracking_url or f"{TRACKING_BASE}/t?id={hash_id}"
            a.save(update_fields=["remote_url", "updated_at"])
            logger.info("Care Panel media DEDUP ticket=%s skip duplicate sha256=%s (%s)",
                        ticket.ticket_id, digest[:12], a.filename)
            continue
        seen.add(digest)
        pending.append(a)

    if not pending:
        return 0

    if session is None:
        import requests
        session = requests.Session()

    try:
        page = session.get(f"{TRACKING_BASE}/t?id={hash_id}", timeout=20)
        m = _TOKEN_RE.search(page.text or "")
        if not m:
            logger.error("Care Panel media %s: CSRF _token not found on tracking page "
                         "(hashId=%s, status=%s).", ticket.ticket_id, hash_id, page.status_code)
            return 0
        token = m.group(1)

        files, used = [], []
        for a in pending:
            prepared = _prepare_file(a)         # convert webp->png, drop unsupported
            if prepared is None:
                a.remote_url = "skipped:unsupported_type"   # don't retry forever
                a.save(update_fields=["remote_url", "updated_at"])
                continue
            fname, content, ctype = prepared
            files.append(("attachments[]", (fname, content, ctype)))
            used.append(a)
        if not files:
            logger.info("Care Panel media ticket=%s: no Care-Panel-compatible files.",
                        ticket.ticket_id)
            return 0
        pending = used
        data = {"_token": token, "hashId": hash_id, "comment": comment or DEFAULT_COMMENT}

        logger.info("Care Panel add_comment UPLOAD ticket=%s hashId=%s files=%d",
                    ticket.ticket_id, hash_id, len(files))
        resp = session.post(
            f"{TRACKING_BASE}/t/add_comment", data=data, files=files,
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            timeout=60,
        )
        logger.info("Care Panel add_comment RESPONSE ticket=%s status=%s",
                    ticket.ticket_id, resp.status_code)

        if resp.status_code not in (200, 201, 302):
            logger.error("Care Panel media upload FAILED ticket=%s status=%s body=%s",
                         ticket.ticket_id, resp.status_code, (resp.text or "")[:400])
            AuditLogEntry.objects.create(
                ticket=ticket, actor="system", event="care_panel_media_failed",
                detail={"status": resp.status_code, "body": (resp.text or "")[:300],
                        "files": len(files)},
            )
            return 0

        marker = ticket.tracking_url or f"{TRACKING_BASE}/t?id={hash_id}"
        for a in pending:
            a.remote_url = marker
            a.save(update_fields=["remote_url", "updated_at"])
        logger.info("Care Panel media UPLOADED ticket=%s files=%d", ticket.ticket_id, len(pending))
        AuditLogEntry.objects.create(
            ticket=ticket, actor="system", event="care_panel_media_uploaded",
            detail={"count": len(pending), "files": [a.filename for a in pending]},
        )
        return len(pending)
    except Exception:  # noqa: BLE001 -- best-effort
        logger.exception("Care Panel media upload ERROR for %s", ticket.ticket_id)
        return 0
