"""
Backfill attachment FILES for messages that only have attachment metadata (e.g.
ingested before file-storage existed, or skipped by dedup). Re-downloads the bytes
over IMAP, matches each message by RFC822 Message-ID, and stores the files.

Usage:
    python manage.py backfill_attachments              # scan recent 200 messages
    python manage.py backfill_attachments --limit 500
    python manage.py backfill_attachments --mailbox care@deodap.com
"""

from django.core.management.base import BaseCommand, CommandError

from apps.ingestion import service
from apps.ingestion.imap_client import ImapClient, parse_rfc822
from apps.organizations.models import Mailbox
from apps.tickets.models import Message


class Command(BaseCommand):
    help = "Re-download and store attachment files for existing messages (IMAP)."

    def add_arguments(self, parser):
        parser.add_argument("--mailbox", help="Mailbox email address.")
        parser.add_argument("--limit", type=int, default=200,
                            help="How many recent IMAP messages to scan.")

    def handle(self, *args, **opts):
        client = ImapClient.from_settings()
        if client is None:
            raise CommandError("IMAP not configured (set IMAP_HOST/IMAP_USER/IMAP_PASSWORD).")
        mailbox = (
            Mailbox.objects.filter(email_address=opts["mailbox"]).first()
            if opts.get("mailbox") else Mailbox.objects.first()
        )

        conn = client._connect()
        try:
            conn.select(client.folder)
            typ, data = conn.uid("search", None, "ALL")
            uids = (data[0].split() if data and data[0] else [])[-opts["limit"]:]
            backfilled = scanned = 0
            for uid in uids:
                typ, md = conn.uid("fetch", uid, "(RFC822)")
                if not md or not md[0]:
                    continue
                msg = parse_rfc822(md[0][1])
                blobs = msg.get("attachment_blobs") or []
                if not blobs:
                    continue
                scanned += 1
                mid = msg.get("message_id")
                m = Message.objects.filter(gmail_message_id=mid).first() if mid else None
                if m is None or m.stored_attachments.exists():
                    continue
                service._store_attachments(m.ticket, m, blobs)
                backfilled += 1
                self.stdout.write(
                    f"  {m.ticket.ticket_id}: stored {len(blobs)} file(s) "
                    f"<- {(msg.get('subject') or '')[:40]}"
                )
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

        self.stdout.write(self.style.SUCCESS(
            f"Backfilled {backfilled} message(s) ({scanned} with attachments scanned)."))
