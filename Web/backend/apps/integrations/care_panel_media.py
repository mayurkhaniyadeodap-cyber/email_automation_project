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
import os
import re
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

TRACKING_BASE = "https://care.deodap.in"
# The Care Panel comment form only accepts these (rejects e.g. webp/gif with 422).
ACCEPTED_TYPES = {"image/jpeg", "image/jpg", "image/png", "video/mp4",
                  "video/quicktime", "application/pdf"}
_TOKEN_RE = re.compile(r'name="_token"\s+value="([^"]+)"')
DEFAULT_COMMENT = "Customer shared photo / video evidence."

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
        page = session.get(f"{TRACKING_BASE}/t?id={hash_id}", timeout=20)
        m = _TOKEN_RE.search(page.text or "")
        if not m:
            logger.error("Care Panel media %s: CSRF _token not found on tracking page "
                         "(hashId=%s, status=%s).", ticket.ticket_id, hash_id, page.status_code)
            return 0
        token = m.group(1)

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
            data = {"_token": token, "hashId": hash_id, "comment": comment or DEFAULT_COMMENT}
            files = [("attachments[]", (fname, content, ctype))]
            logger.info("Care Panel add_comment UPLOAD ticket=%s hashId=%s file=%s size=%d",
                        ticket.ticket_id, hash_id, fname, len(content))
            try:
                resp = session.post(
                    f"{TRACKING_BASE}/t/add_comment", data=data, files=files,
                    headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
                    timeout=120,
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
