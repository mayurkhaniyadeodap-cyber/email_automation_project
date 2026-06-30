"""
Ingest a sample Gmail message JSON (a `users.messages.get` dump) into a mailbox
WITHOUT a live Gmail connection -- handy for local demos and for eyeballing the
ignore gate / threading end-to-end.

Usage:
    python manage.py ingest_sample --mailbox care@deodap.com --file sample.json

The JSON file is a single Gmail message resource (or a list of them).
"""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.ingestion.normalize import parse_gmail_message
from apps.ingestion.service import ingest_message
from apps.organizations.models import Mailbox


class Command(BaseCommand):
    help = "Ingest a sample Gmail message JSON file (offline, no live Gmail)."

    def add_arguments(self, parser):
        parser.add_argument("--mailbox", required=True, help="Mailbox email address.")
        parser.add_argument("--file", required=True, help="Path to the message JSON.")

    def handle(self, *args, **opts):
        mailbox = Mailbox.objects.filter(email_address=opts["mailbox"]).first()
        if not mailbox:
            raise CommandError(f"No mailbox {opts['mailbox']}.")

        with open(opts["file"], encoding="utf-8") as fh:
            data = json.load(fh)
        raw_messages = data if isinstance(data, list) else [data]

        for raw in raw_messages:
            normalized = parse_gmail_message(raw)
            ticket, msg, created = ingest_message(mailbox, normalized)
            verb = "created" if created else "duplicate"
            flag = " [IGNORED]" if ticket.is_ignored else ""
            self.stdout.write(
                f"{verb}: {ticket.ticket_id}{flag} <- {normalized['from_email']} "
                f"({normalized['subject']})"
            )
