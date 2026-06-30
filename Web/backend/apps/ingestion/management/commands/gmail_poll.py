"""
Fallback poll (doc section 2, "Fallback: a cron job every 2-3 min runs
history.list in case a Pub/Sub push was missed. Belt and braces.")

Usage:
    python manage.py gmail_poll                 # all active, authorized mailboxes
    python manage.py gmail_poll --mailbox care@deodap.com
"""

from django.core.management.base import BaseCommand

from apps.ingestion.service import build_client, sync_history
from apps.organizations.models import Mailbox


class Command(BaseCommand):
    help = "Poll Gmail history.list for missed pushes (run every 2-3 min)."

    def add_arguments(self, parser):
        parser.add_argument("--mailbox", help="Limit to one mailbox email address.")

    def handle(self, *args, **opts):
        mailboxes = Mailbox.objects.filter(is_active=True)
        if opts.get("mailbox"):
            mailboxes = mailboxes.filter(email_address=opts["mailbox"])

        total = 0
        for mailbox in mailboxes:
            client = build_client(mailbox)
            if client is None:
                continue
            results = sync_history(mailbox, client=client)
            total += len(results)
            self.stdout.write(
                f"{mailbox.email_address}: ingested {len(results)}"
            )
        self.stdout.write(self.style.SUCCESS(f"Total ingested: {total}"))
