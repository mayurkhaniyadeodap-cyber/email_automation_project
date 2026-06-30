"""
Promote pending conversations that ALREADY satisfy the rules into real tickets.

Useful after a logic change (or a stale server) left a conversation stuck in
awaiting_evidence even though the required evidence + an identifier are present --
e.g. the customer already sent the mandatory video but the old code kept asking for
an Order ID. New rule: evidence satisfied + ANY identifier (email/phone/order) -> ticket.

    python manage.py flush_pending            # promote all ready pendings
    python manage.py flush_pending --id 11    # just one
    python manage.py flush_pending --dry-run  # show what would promote
"""

from django.core.management.base import BaseCommand

from apps.ingestion import evidence, service
from apps.tickets.models import PendingConversation


def _is_ready(pending):
    level = service._pending_evidence_level(pending)
    if level == evidence.EV_VIDEO and not pending.has_video:
        return False, "needs video"
    if level == evidence.EV_PHOTO and not pending.has_evidence:
        return False, "needs photo"
    if not service._has_identifier(pending):
        return False, "no identifier"
    return True, "ready"


class Command(BaseCommand):
    help = "Promote pending conversations that already satisfy evidence + identifier."

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, help="Only this pending conversation id.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would be promoted, change nothing.")

    def handle(self, *args, **opts):
        qs = PendingConversation.objects.exclude(status="closed")
        if opts.get("id"):
            qs = qs.filter(id=opts["id"])

        promoted = skipped = 0
        for pending in qs.order_by("id"):
            ready, why = _is_ready(pending)
            if not ready:
                self.stdout.write(f"  skip #{pending.id} ({pending.customer_email}): {why}")
                skipped += 1
                continue
            if opts.get("dry_run"):
                self.stdout.write(self.style.WARNING(
                    f"  WOULD promote #{pending.id} ({pending.customer_email})"))
                promoted += 1
                continue
            ticket = self._promote(pending)
            self.stdout.write(self.style.SUCCESS(
                f"  promoted #{pending.id} -> {ticket.ticket_id} "
                f"({ticket.customer_email}) tracking={ticket.tracking_url or '(none)'}"))
            promoted += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done: {promoted} promoted, {skipped} skipped."))

    def _promote(self, pending):
        # Synthetic reply so _promote_pending can recreate the thread + finalize. The
        # accumulated attachments already live on the pending and are moved to the ticket.
        message = {
            "from_email": pending.customer_email,
            "subject": f"Re: {pending.subject}" if pending.subject else "Re: your request",
            "body_text": "", "body_html": "",
            "message_id": f"<flush-{pending.id}@internal>",
            "gmail_message_id": f"flush-{pending.id}",
            "thread_id": pending.thread_id or pending.original_message_id,
            "in_reply_to": pending.last_message_id,
            "references": pending.references or [],
            "headers": {},
        }
        return service._promote_pending(pending.mailbox, pending, message)
