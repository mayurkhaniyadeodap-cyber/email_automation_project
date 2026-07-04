"""
Upload customer photo/video attachments to the DeoDap Care Panel so they show under
"Media Files" on the public tracking page (care.deodap.in/t?id=...).

Mechanism (discovered + verified live against the tracking page's comment form):

    GET  https://care.deodap.in/t?id=<hashId>     -> XSRF-TOKEN + laravel_session cookies
    POST https://care.deodap.in/t/add_comment     (multipart/form-data)
         header X-XSRF-TOKEN=<urldecoded XSRF-TOKEN cookie>   (Laravel cookie CSRF)
         hashId=<hash>  comment=<text>  attachments[]=<file>...

NOTE: the panel dropped the server-rendered ``name="_token"`` hidden field; it now uses
the cookie-based CSRF above. Sending the old ``_token`` (or none) returns HTTP 419 and the
comment is silently discarded. See ``_csrf()``.

The hashId is the open-tickets `id` (the t?id= value), stored on the ticket as
extracted["care_panel_ticket_id"]. Accepted types: image/jpeg, image/png, video/mp4,
video/quicktime, application/pdf.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse

logger = logging.getLogger(__name__)

TRACKING_BASE = "https://care.deodap.in"
# The Care Panel comment form only accepts these (rejects e.g. webp/gif with 422).
ACCEPTED_TYPES = {"image/jpeg", "image/jpg", "image/png", "video/mp4",
                  "video/quicktime", "application/pdf"}
_TOKEN_RE = re.compile(r'name="_token"\s+value="([^"]+)"')
DEFAULT_COMMENT = "Customer shared photo / video evidence."


def _csrf(session, hash_id):
    """Establish a CSRF-authenticated session for POST /t/add_comment and return
    ``(headers, data_extra, ok, status)``.

    The external Care Panel (Laravel) NO LONGER renders a ``name="_token"`` hidden
    field on the /t page -- it now relies on the framework's cookie CSRF: the
    ``XSRF-TOKEN`` cookie handed out on the GET must be echoed back (URL-decoded)
    in the ``X-XSRF-TOKEN`` request header. Sending the old ``_token`` form field
    (or nothing) now yields HTTP 419 "CSRF token mismatch" and the comment is never
    stored -- which silently broke BOTH media uploads and conversation sync.

    We GET the page first so the session captures ``XSRF-TOKEN`` + ``laravel_session``
    cookies, prefer the cookie->header path (proven to pass CSRF), and still fold in a
    scraped ``_token`` as a body field when the page happens to expose one (older panel
    builds) so this works against both versions."""
    page = session.get(f"{TRACKING_BASE}/t?id={hash_id}", timeout=20)
    headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
    data_extra = {}
    xsrf = None
    try:
        xsrf = session.cookies.get("XSRF-TOKEN")
    except Exception:  # noqa: BLE001 -- some fake sessions have no cookie jar
        xsrf = None
    if xsrf:
        headers["X-XSRF-TOKEN"] = urllib.parse.unquote(xsrf)
    m = _TOKEN_RE.search(page.text or "")
    if m:
        data_extra["_token"] = m.group(1)          # legacy hidden field, still honoured if present
    ok = bool(xsrf or data_extra)
    return headers, data_extra, ok, page.status_code

# The external Care Panel's nginx caps the upload body at ~1 MB (verified by probe). Target
# the compressed video safely under that -- the multipart form fields + boundaries also count
# toward the body, so leave headroom.
CARE_PANEL_MAX_BYTES = 950_000
_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")


def _video_duration(path):
    if not _FFPROBE:
        return 0.0
    try:
        out = subprocess.run(
            [_FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        return float((out.stdout or "0").strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _compress_video(content, target_bytes=CARE_PANEL_MAX_BYTES):
    """Re-encode a video down to <= target_bytes (H.264/AAC, progressively scaled down) so it
    fits the external Care Panel's upload limit. Budgets the bitrate from the clip duration and
    steps through resolution/audio ladders until it fits. Returns the compressed bytes, or None
    if it can't get under the target (e.g. a very long clip). Best-effort; never raises."""
    if not _FFMPEG:
        return None
    tmpd = tempfile.mkdtemp(prefix="cpv_")
    src = os.path.join(tmpd, "in")
    try:
        with open(src, "wb") as fh:
            fh.write(content)
        dur = _video_duration(src)
        # (max_width, audio_kbps) -- least aggressive first; last entry drops audio.
        for width, a_kbps in ((640, 64), (480, 48), (360, 32), (320, 0)):
            if dur > 0:
                total_kbps = (target_bytes * 8 / 1000) / dur
                v_kbps = int(max(120, total_kbps - a_kbps) * 0.9)   # 10% headroom
            else:
                v_kbps = 600
            out = os.path.join(tmpd, f"out_{width}.mp4")
            cmd = [_FFMPEG, "-y", "-i", src,
                   "-vf", f"scale='min({width},iw)':-2",
                   "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "baseline",
                   "-b:v", f"{v_kbps}k", "-maxrate", f"{int(v_kbps * 1.2)}k",
                   "-bufsize", f"{v_kbps * 2}k", "-movflags", "+faststart"]
            cmd += (["-c:a", "aac", "-b:a", f"{a_kbps}k"] if a_kbps else ["-an"])
            cmd += [out]
            try:
                subprocess.run(cmd, capture_output=True, timeout=180, check=True)
            except Exception:  # noqa: BLE001
                continue
            if os.path.exists(out) and 0 < os.path.getsize(out) <= target_bytes:
                with open(out, "rb") as fh:
                    return fh.read()
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Care Panel video compression failed")
        return None
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


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
        headers, csrf_data, ok, status = _csrf(session, hash_id)
        if not ok:
            logger.error("Care Panel media %s: no CSRF (XSRF-TOKEN cookie / _token) on tracking "
                         "page (hashId=%s, status=%s).", ticket.ticket_id, hash_id, status)
            return 0

        # Upload ONE FILE PER REQUEST. The external Care Panel's nginx caps the request body
        # (client_max_body_size), so a single multi-MB video 413s -- and batching all files in
        # one POST made the WHOLE upload fail (the small image was lost with it). Per-file means
        # each attachment is judged on its own: small files still get through even when a large
        # one is rejected, and an oversized file fails in isolation (logged, not silently dropped).
        marker = ticket.tracking_url or f"{TRACKING_BASE}/t?id={hash_id}"
        uploaded = 0
        for a in pending:
            prepared = _prepare_file(a)         # convert webp->png, drop unsupported
            if prepared is None:
                a.remote_url = "skipped:unsupported_type"   # don't retry forever
                a.save(update_fields=["remote_url", "updated_at"])
                continue
            fname, content, ctype = prepared
            # Videos over the external 1 MB cap are re-encoded smaller so they still fit; if it
            # can't be shrunk enough we still attempt the original (it 413s, gets logged, and is
            # left pending for retry if the care.deodap.in limit is later raised).
            if ctype.startswith("video/") and len(content) > CARE_PANEL_MAX_BYTES:
                comp = _compress_video(content)
                if comp and len(comp) < len(content):
                    logger.info("Care Panel media: compressed video %s %d -> %d bytes to fit limit.",
                                fname, len(content), len(comp))
                    content = comp
                else:
                    logger.warning("Care Panel media: could not shrink %s under %d bytes; "
                                   "attempting original (expect 413 until care.deodap.in raises "
                                   "client_max_body_size).", fname, CARE_PANEL_MAX_BYTES)
            data = {"hashId": hash_id, "comment": comment or DEFAULT_COMMENT, **csrf_data}
            files = [("attachments[]", (fname, content, ctype))]
            logger.info("Care Panel add_comment UPLOAD ticket=%s hashId=%s file=%s size=%d",
                        ticket.ticket_id, hash_id, fname, len(content))
            try:
                resp = session.post(
                    f"{TRACKING_BASE}/t/add_comment", data=data, files=files,
                    headers=headers, timeout=120,
                )
            except Exception:  # noqa: BLE001 -- one bad file must not abort the rest
                logger.exception("Care Panel media POST ERROR ticket=%s file=%s",
                                 ticket.ticket_id, fname)
                continue
            logger.info("Care Panel add_comment RESPONSE ticket=%s file=%s status=%s",
                        ticket.ticket_id, fname, resp.status_code)
            if resp.status_code not in (200, 201, 302):
                too_large = resp.status_code == 413
                logger.error(
                    "Care Panel media upload FAILED ticket=%s file=%s size=%d status=%s%s body=%s",
                    ticket.ticket_id, fname, len(content), resp.status_code,
                    " -> file exceeds the Care Panel nginx client_max_body_size; raise it on the "
                    "care.deodap.in server" if too_large else "", (resp.text or "")[:200])
                AuditLogEntry.objects.create(
                    ticket=ticket, actor="system", event="care_panel_media_failed",
                    detail={"file": fname, "size": len(content),
                            "status": resp.status_code, "too_large": too_large},
                )
                continue  # leave remote_url="" so it retries if the limit is later raised
            a.remote_url = marker
            a.save(update_fields=["remote_url", "updated_at"])
            uploaded += 1
            logger.info("Care Panel media UPLOADED ticket=%s file=%s", ticket.ticket_id, fname)

        if uploaded:
            AuditLogEntry.objects.create(
                ticket=ticket, actor="system", event="care_panel_media_uploaded",
                detail={"count": uploaded},
            )
        return uploaded
    except Exception:  # noqa: BLE001 -- best-effort
        logger.exception("Care Panel media upload ERROR for %s", ticket.ticket_id)
        return 0


def sync_conversation(ticket, session=None):
    """Push the ticket's email conversation into the Care Panel thread.

    WHY THIS ENDPOINT: the store-json create API has NO conversation/messages/chat field (see
    care_panel_store._payload), and the panel exposes NO separate agent-reply endpoint. The ONLY
    thread-write endpoint is POST /t/add_comment (the customer comment form used for media) --
    which has NO sender field, so every comment renders customer-side. We therefore label the
    sender INLINE in the comment text.

    The ticket-creating email is already shown in the thread as the store-json `detail`, so the
    FIRST customer message is skipped (marked synced, not re-posted). Every other message is
    posted ONCE; already-synced ids are tracked in extracted['cp_synced_messages'] so re-runs
    (and future replies) never duplicate. Best-effort; never raises. Returns messages posted."""
    from apps.integrations.care_panel_store import _conversation_payload
    from apps.tickets.models import AuditLogEntry

    hash_id = (ticket.extracted or {}).get("care_panel_ticket_id")
    if not hash_id:
        logger.info("Care Panel conversation SKIP %s: no care_panel_ticket_id (hashId) yet.",
                    ticket.ticket_id)
        return 0

    ex = dict(ticket.extracted or {})
    synced = set(ex.get("cp_synced_messages") or [])
    convo = _conversation_payload(ticket)
    # The first customer email == the store-json 'detail' already in the thread -> skip it once.
    first_customer_id = next((c["_id"] for c in convo if c["sender"] == "Customer"), None)
    pending = [c for c in convo
               if c["_id"] not in synced and c["_id"] != first_customer_id]
    if first_customer_id is not None and first_customer_id not in synced:
        synced.add(first_customer_id)          # represented by 'detail', never posted
    if not pending:
        if synced != set(ex.get("cp_synced_messages") or []):
            ex["cp_synced_messages"] = sorted(synced)
            ticket.extracted = ex
            ticket.save(update_fields=["extracted", "updated_at"])
        return 0

    if session is None:
        import requests
        session = requests.Session()

    posted = 0
    try:
        headers, csrf_data, ok, status = _csrf(session, hash_id)
        if not ok:
            logger.error("Care Panel conversation %s: no CSRF (XSRF-TOKEN cookie / _token) "
                         "(hashId=%s, status=%s).", ticket.ticket_id, hash_id, status)
            return 0
        for c in pending:
            label = "🟢 Customer" if c["sender"] == "Customer" else "🔵 DeoDap Support"
            subj = f"Subject: {c['subject']}\n" if c["subject"] else ""
            comment = f"[{label}]\n{subj}{c['message']}".strip()
            try:
                resp = session.post(
                    f"{TRACKING_BASE}/t/add_comment",
                    data={"hashId": hash_id, "comment": comment, **csrf_data},
                    headers=headers, timeout=60)
            except Exception:  # noqa: BLE001 -- one bad message must not abort the rest
                logger.exception("Care Panel conversation POST error ticket=%s msg=%s",
                                 ticket.ticket_id, c["_id"])
                continue
            if resp.status_code in (200, 201, 302):
                synced.add(c["_id"])
                posted += 1
            else:
                logger.error("Care Panel conversation FAILED ticket=%s msg=%s status=%s body=%s",
                             ticket.ticket_id, c["_id"], resp.status_code, (resp.text or "")[:200])
    except Exception:  # noqa: BLE001 -- best-effort; never block on the thread sync
        logger.exception("Care Panel conversation sync ERROR for %s", ticket.ticket_id)

    ex["cp_synced_messages"] = sorted(synced)
    ticket.extracted = ex
    ticket.save(update_fields=["extracted", "updated_at"])
    if posted:
        logger.info("Care Panel conversation SYNCED ticket=%s messages=%d (hashId=%s)",
                    ticket.ticket_id, posted, hash_id)
        AuditLogEntry.objects.create(ticket=ticket, actor="system",
                                     event="care_panel_conversation_synced",
                                     detail={"count": posted})
    return posted
