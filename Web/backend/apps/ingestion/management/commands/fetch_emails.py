"""
Fetch new mail into tickets using the configured provider (settings.EMAIL_PROVIDER):
IMAP (default) or the Gmail API. Run on a schedule (cron / Celery beat) or by hand.

Usage:
    python manage.py fetch_emails                       # all active mailboxes
    python manage.py fetch_emails --mailbox care@deodap.com
    python manage.py fetch_emails --debug               # non-destructive IMAP dump
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.ingestion import service
from apps.organizations.models import Mailbox


class Command(BaseCommand):
    help = "Fetch new emails into tickets (IMAP or Gmail API)."

    def add_arguments(self, parser):
        parser.add_argument("--mailbox", help="Limit to one mailbox email address.")
        parser.add_argument("--debug", action="store_true",
                            help="Non-destructive IMAP dump: per-message UID / Message-ID / "
                                 "From / Subject / Date / Seen, plus the watermark + "
                                 "fetch-limit skip analysis. Does NOT mark seen or ingest.")
        parser.add_argument("--debug-count", type=int, default=40,
                            help="How many most-recent UIDs to dump with --debug.")

    def handle(self, *args, **opts):
        provider = getattr(settings, "EMAIL_PROVIDER", "imap")
        mailboxes = Mailbox.objects.filter(is_active=True)
        if opts.get("mailbox"):
            mailboxes = mailboxes.filter(email_address=opts["mailbox"])

        if opts.get("debug"):
            for mailbox in mailboxes:
                self._debug_dump(mailbox, opts["debug_count"])
            return

        total = 0
        for mailbox in mailboxes:
            if provider == "imap":
                results = service.fetch_imap(mailbox)
            else:
                from apps.ingestion.service import build_client

                if build_client(mailbox) is None:
                    self.stdout.write(self.style.WARNING(
                        f"Skip {mailbox.email_address}: Gmail not authorized."))
                    continue
                results = service.sync_history(mailbox)
            total += len(results)
            self.stdout.write(f"{mailbox.email_address}: ingested {len(results)}")
        self.stdout.write(self.style.SUCCESS(
            f"[{provider}] total ingested: {total}"))

    # ------------------------------------------------------------------ #
    # Temporary IMAP diagnostics (non-destructive: BODY.PEEK, no ingest)  #
    # ------------------------------------------------------------------ #
    def _debug_dump(self, mailbox, count):
        import email
        import imaplib
        import re
        from email import policy

        try:
            self.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        host, user, pw = settings.IMAP_HOST, settings.IMAP_USER, settings.IMAP_PASSWORD
        folder = getattr(settings, "IMAP_FOLDER", "INBOX")
        limit = getattr(settings, "IMAP_FETCH_LIMIT", 25)
        w = self.stdout.write
        w(f"\n=== IMAP DEBUG: mailbox={mailbox.email_address} (polls {user}) ===")
        if not host or not user:
            w(self.style.ERROR("IMAP not configured.")); return

        conn = imaplib.IMAP4_SSL(host, settings.IMAP_PORT)
        try:
            conn.login(user, pw)
            typ, st = conn.status(folder, "(UIDVALIDITY UIDNEXT MESSAGES)")
            w(f"server   : {st[0].decode(errors='replace')}")
            w(f"stored   : imap_last_uid={mailbox.imap_last_uid} "
              f"imap_uidvalidity={mailbox.imap_uidvalidity} IMAP_FETCH_LIMIT={limit}")
            conn.select(folder)
            last = int(mailbox.imap_last_uid or 0)

            # What the next real fetch would search for + the [-limit:] skip analysis.
            crit = f"(UID {last + 1}:*)" if last else "UNSEEN"
            typ, data = conn.uid("search", None, crit)
            cand = sorted({int(u) for u in (data[0].split() if data and data[0] else [])
                           if not last or int(u) > last})
            kept, dropped = cand[-limit:], (cand[:-limit] if len(cand) > limit else [])
            w(f"\nnext fetch search = {crit}")
            w(f"  candidates new UIDs : {cand}")
            w(self.style.WARNING(f"  KEPT (top {limit})     : {kept}"))
            w(self.style.ERROR(f"  DROPPED by [-limit:]   : {dropped}  (permanently skipped)"))

            # Per-message dump of the most-recent `count` UIDs (PEEK -> no \Seen set).
            typ, data = conn.uid("search", None, "ALL")
            all_uids = sorted(int(u) for u in (data[0].split() if data and data[0] else []))
            w(f"\nper-message dump (last {count} UIDs, non-destructive):")
            w(f"  {'UID':>5} {'SEEN':<6} {'DATE':<31} FROM | SUBJECT")
            for uid in all_uids[-count:]:
                typ, d = conn.uid("fetch", str(uid),
                                  "(FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS "
                                  "(MESSAGE-ID FROM SUBJECT DATE)])")
                flags, mid, frm, subj, dt = "", "", "", "", ""
                for part in d:
                    if isinstance(part, tuple):
                        meta = part[0].decode(errors="replace")
                        fm = re.search(r"FLAGS \(([^)]*)\)", meta)
                        flags = fm.group(1) if fm else ""
                        m = email.message_from_bytes(part[1], policy=policy.default)
                        mid, frm = m.get("Message-ID", ""), m.get("From", "")
                        subj, dt = m.get("Subject", ""), m.get("Date", "")
                seen = "SEEN" if "Seen" in flags else "UNSEEN"
                w(f"  {uid:>5} {seen:<6} {dt[:31]:<31} {frm[:28]} | {subj[:34]}")
                w(f"        msgid={mid}")
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
