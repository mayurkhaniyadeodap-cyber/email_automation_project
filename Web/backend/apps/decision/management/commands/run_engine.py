"""
Run the decision engine over tickets (doc section 5). Useful to apply the engine to
tickets classified before the engine existed, or to re-run after editing rules.

Usage:
    python manage.py run_engine                       # all 'classified' tickets
    python manage.py run_engine --status new
    python manage.py run_engine --ticket TKT-2026-000123
    python manage.py run_engine --brand 1 --all
"""

from django.core.management.base import BaseCommand

from apps.decision import engine
from apps.integrations import context as live_context
from apps.tickets.models import Ticket


class Command(BaseCommand):
    help = "Run the IF/THEN/Action decision engine over tickets."

    def add_arguments(self, parser):
        parser.add_argument("--brand", type=int, help="Limit to a brand id.")
        parser.add_argument("--ticket", help="A single ticket_id.")
        parser.add_argument(
            "--status", default=Ticket.STATUS_CLASSIFIED,
            help="Filter by status (default: classified). Ignored with --ticket/--all.",
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

        decided = 0
        for ticket in qs.select_related("brand", "sub_topic_ref", "mailbox"):
            facts = live_context.build_context(ticket)
            plan = engine.run(ticket, context=facts)
            if plan is None:
                continue
            decided += 1
            self.stdout.write(
                f"{ticket.ticket_id}: {plan.action_code} -> {plan.send_mode} "
                f"({plan.status}, {plan.priority})"
                + (f" [{', '.join(plan.reasons)}]" if plan.reasons else "")
            )
        self.stdout.write(self.style.SUCCESS(f"Decided {decided} ticket(s)."))
