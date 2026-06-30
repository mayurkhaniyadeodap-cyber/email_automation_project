"""
(Re)register the Gmail Pub/Sub watch for each active, authorized mailbox
(doc section 2). The watch expires every 7 days, so run this DAILY (cron) to keep
push notifications flowing.

Usage:
    python manage.py gmail_watch                 # all active mailboxes
    python manage.py gmail_watch --mailbox care@deodap.com
"""

from datetime import timezone as dt_timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.ingestion.service import build_client
from apps.organizations.models import Mailbox


class Command(BaseCommand):
    help = "Call users.watch for active mailboxes so Gmail pushes to Pub/Sub."

    def add_arguments(self, parser):
        parser.add_argument("--mailbox", help="Limit to one mailbox email address.")

    def handle(self, *args, **opts):
        topic = settings.GMAIL_PUBSUB_TOPIC
        if not topic:
            raise CommandError("GMAIL_PUBSUB_TOPIC is not set in the environment.")

        mailboxes = Mailbox.objects.filter(is_active=True)
        if opts.get("mailbox"):
            mailboxes = mailboxes.filter(email_address=opts["mailbox"])

        watched = 0
        for mailbox in mailboxes:
            client = build_client(mailbox)
            if client is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"Skip {mailbox.email_address}: not authorized "
                        f"(run gmail_authorize first)."
                    )
                )
                continue
            resp = client.start_watch(topic)
            mailbox.gmail_history_id = str(resp.get("historyId", mailbox.gmail_history_id))
            expiration = resp.get("expiration")
            if expiration:
                # Gmail returns expiration as epoch millis.
                mailbox.watch_expiry = timezone.datetime.fromtimestamp(
                    int(expiration) / 1000, tz=dt_timezone.utc
                )
            mailbox.save(
                update_fields=["gmail_history_id", "watch_expiry", "updated_at"]
            )
            watched += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"Watching {mailbox.email_address} "
                    f"(historyId={mailbox.gmail_history_id}, expires {mailbox.watch_expiry})."
                )
            )
        self.stdout.write(self.style.SUCCESS(f"Watches (re)registered: {watched}"))
