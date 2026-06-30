"""
Classify (or re-classify) tickets with the brand AI provider (doc section 4).
Useful to backfill tickets ingested before AI was configured, or to re-run after
editing the taxonomy / switching models.

Usage:
    python manage.py classify_tickets                       # all 'new' tickets, all brands
    python manage.py classify_tickets --brand 1 --status new
    python manage.py classify_tickets --ticket TKT-2026-000123
    python manage.py classify_tickets --all                 # every non-ignored ticket
"""

from django.core.management.base import BaseCommand

from apps.classifier import service as classifier
from apps.tickets.models import Ticket


class Command(BaseCommand):
    help = "Run the AI classifier over tickets."

    def add_arguments(self, parser):
        parser.add_argument("--brand", type=int, help="Limit to a brand id.")
        parser.add_argument("--ticket", help="A single ticket_id.")
        parser.add_argument(
            "--status", default=Ticket.STATUS_NEW,
            help="Filter by status (default: new). Ignored with --ticket/--all.",
        )
        parser.add_argument(
            "--all", action="store_true",
            help="Every non-ignored ticket regardless of status.",
        )

    def handle(self, *args, **opts):
        qs = Ticket.objects.filter(is_ignored=False)
        if opts.get("ticket"):
            qs = qs.filter(ticket_id=opts["ticket"])
        elif not opts.get("all"):
            qs = qs.filter(status=opts["status"])
        if opts.get("brand"):
            qs = qs.filter(brand_id=opts["brand"])

        classified = skipped = 0
        for ticket in qs.select_related("brand"):
            result = classifier.classify_ticket(ticket)
            if result is None:
                skipped += 1
                continue
            classified += 1
            self.stdout.write(
                f"{ticket.ticket_id}: {result.category} / {result.sub_topic} "
                f"(conf {result.confidence:.2f})"
            )
        self.stdout.write(self.style.SUCCESS(
            f"Classified {classified}, skipped {skipped} (no AI provider)."
        ))
