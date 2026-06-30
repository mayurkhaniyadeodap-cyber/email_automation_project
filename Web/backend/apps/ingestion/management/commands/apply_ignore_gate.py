"""
Re-run the Ignore/Block gate over EXISTING non-ignored tickets (doc section 3).

The gate normally runs once at ingest time. After editing a brand's block list in
Settings you can sweep already-ingested tickets so newly-blocked senders/domains get
moved to the Ignored tab too ("stop junk early").

Usage:
    python manage.py apply_ignore_gate                 # all brands, dry run
    python manage.py apply_ignore_gate --commit        # actually ignore matches
    python manage.py apply_ignore_gate --brand 1 --commit
"""

from django.core.management.base import BaseCommand

from apps.ingestion import ignore_gate
from apps.tickets.models import AuditLogEntry, Message, Ticket


def message_dict_from_ticket(ticket):
    """Build the gate's input from a ticket's first inbound mail (sender + headers)."""
    msg = (
        ticket.messages.filter(direction=Message.DIRECTION_INBOUND)
        .order_by("created_at")
        .first()
    )
    if msg is None:
        return {"from_email": ticket.customer_email, "headers": {}}
    return {"from_email": msg.from_email or ticket.customer_email, "headers": msg.headers or {}}


class Command(BaseCommand):
    help = "Re-apply the per-brand Ignore/Block gate to existing open tickets."

    def add_arguments(self, parser):
        parser.add_argument("--brand", type=int, help="Limit to a brand id.")
        parser.add_argument(
            "--commit", action="store_true",
            help="Apply changes (default is a dry run that only reports matches).",
        )

    def handle(self, *args, **opts):
        qs = Ticket.objects.filter(is_ignored=False).select_related("brand")
        if opts.get("brand"):
            qs = qs.filter(brand_id=opts["brand"])

        matched = 0
        for ticket in qs:
            result = ignore_gate.evaluate(ticket.brand, message_dict_from_ticket(ticket))
            if not result.ignored:
                continue
            matched += 1
            self.stdout.write(f"{ticket.ticket_id}: {result.reason}")
            if opts.get("commit"):
                ticket.is_ignored = True
                ticket.ignored_reason = result.reason
                ticket.status = Ticket.STATUS_IGNORED
                ticket.save(
                    update_fields=["is_ignored", "ignored_reason", "status", "updated_at"]
                )
                AuditLogEntry.objects.create(
                    ticket=ticket, actor="system", event="ignored",
                    detail={"reason": result.reason, "retro": True},
                )

        verb = "Ignored" if opts.get("commit") else "Would ignore"
        self.stdout.write(self.style.SUCCESS(f"{verb} {matched} ticket(s)."))
