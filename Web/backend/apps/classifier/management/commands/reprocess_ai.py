"""
Reprocess tickets whose AI classification failed or is still pending (spec rule 3:
"if Gemini returns 429, queue email for reprocessing"). Run this on a schedule
(cron / Celery beat) or by hand once the Gemini quota resets.

Usage:
    python manage.py reprocess_ai                 # AI_FAILED + PENDING_AI tickets
    python manage.py reprocess_ai --status AI_FAILED
    python manage.py reprocess_ai --brand 1 --limit 50
"""

from django.core.management.base import BaseCommand

from apps.classifier import service as classifier
from apps.decision import engine
from apps.integrations import context as live_context
from apps.tickets.models import Ticket


class Command(BaseCommand):
    help = "Re-run AI classification on failed / pending tickets."

    def add_arguments(self, parser):
        parser.add_argument("--brand", type=int, help="Limit to a brand id.")
        parser.add_argument("--status", help="AI_FAILED or PENDING_AI (default: both).")
        parser.add_argument("--limit", type=int, default=100, help="Max tickets per run.")

    def handle(self, *args, **opts):
        statuses = (
            [opts["status"]] if opts.get("status")
            else [Ticket.CLS_FAILED, Ticket.CLS_PENDING]
        )
        qs = Ticket.objects.filter(
            is_ignored=False, classification_status__in=statuses
        ).select_related("brand")
        if opts.get("brand"):
            qs = qs.filter(brand_id=opts["brand"])
        qs = qs[: opts["limit"]]

        ok = failed = 0
        for ticket in qs:
            result = classifier.classify_ticket(ticket)
            ticket.refresh_from_db()
            if ticket.classification_status == Ticket.CLS_CLASSIFIED:
                ok += 1
                if not ticket.is_ignored:
                    engine.run(ticket, context=live_context.build_context(ticket))
            else:
                failed += 1
            self.stdout.write(
                f"{ticket.ticket_id}: {ticket.classification_status}"
                + (f" ({ticket.ai_error[:60]})" if ticket.ai_error else "")
            )
        self.stdout.write(self.style.SUCCESS(
            f"Reprocessed: {ok} classified, {failed} still failing."))
