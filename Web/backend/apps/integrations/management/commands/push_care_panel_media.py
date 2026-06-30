"""
Push a ticket's photo/video attachments to its Care Panel tracking page so they show
under "Media Files". Runs the open-tickets match first (to resolve the hashId), then
uploads via /t/add_comment.

    python manage.py push_care_panel_media --ticket TKT-2026-000042
    python manage.py push_care_panel_media --all          # every ticket with attachments + a hashId
"""

from django.core.management.base import BaseCommand

from apps.integrations import care_panel, care_panel_media
from apps.tickets.models import Ticket


class Command(BaseCommand):
    help = "Upload ticket attachments to the Care Panel tracking page (Media Files)."

    def add_arguments(self, parser):
        parser.add_argument("--ticket", help="ticket_id, e.g. TKT-2026-000042")
        parser.add_argument("--all", action="store_true",
                            help="all tickets with attachments not yet pushed")

    def handle(self, *args, **o):
        if o.get("ticket"):
            qs = Ticket.objects.filter(ticket_id=o["ticket"])
        elif o.get("all"):
            qs = Ticket.objects.filter(attachments__remote_url="").distinct()
        else:
            self.stderr.write("Pass --ticket <id> or --all."); return

        for t in qs:
            # Resolve the Care Panel hashId via the open-tickets match if we don't have it.
            if not (t.extracted or {}).get("care_panel_ticket_id"):
                care_panel.sync_ticket(t)
                t.refresh_from_db()
            n = care_panel_media.upload_attachments(t)
            self.stdout.write(f"{t.ticket_id}: uploaded {n} media file(s)")
