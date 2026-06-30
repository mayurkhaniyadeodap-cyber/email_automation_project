"""
Re-store tickets that fell back to an INTERNAL tracking link because store-json
transiently failed (e.g. the "Something went wrong, Please try again later." 400).
Each is re-POSTed to the Care Panel; on success the ticket gets a real
https://care.deodap.in/t?id=<hash> link and its internal_tracking flag is cleared.

    python manage.py restore_care_panel                 # all internal-tracking tickets
    python manage.py restore_care_panel --ticket TKT-2026-000061
    python manage.py restore_care_panel --dry-run
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.tickets.models import Ticket


class Command(BaseCommand):
    help = "Re-store internal-tracking tickets in the Care Panel to get real links."

    def add_arguments(self, parser):
        parser.add_argument("--ticket", default="", help="Restore a single TKT id.")
        parser.add_argument("--dry-run", action="store_true",
                            help="List what would be re-stored without calling the API.")
        parser.add_argument("--limit", type=int, default=0, help="Max tickets to process.")

    def handle(self, *args, **o):
        from apps.integrations import care_panel_store
        from apps.ingestion import service

        qs = Ticket.objects.all().order_by("created_at")
        if o["ticket"]:
            qs = qs.filter(ticket_id=o["ticket"])
        else:
            # Tickets with no REAL Care Panel hash: internal fallback, or no link at all.
            qs = qs.filter(
                Q(extracted__internal_tracking=True)
                | Q(tracking_url="")
            ).exclude(extracted__care_panel_ticket_id__gt="")
        if o["limit"]:
            qs = qs[: o["limit"]]

        total = qs.count()
        self.stdout.write(f"{total} ticket(s) to re-store.")
        restored = skipped = failed = 0
        for t in qs:
            phone = (t.extracted or {}).get("phone") or ""
            if not phone:
                self.stdout.write(f"  SKIP  {t.ticket_id}: no phone (store-json is phone-keyed)")
                skipped += 1
                continue
            if o["dry_run"]:
                self.stdout.write(f"  DRY   {t.ticket_id}: would re-store (phone={phone}, "
                                  f"current={t.tracking_url or '(none)'})")
                continue
            care_panel_store.store_ticket(t)
            t.refresh_from_db()
            service._ensure_tracking(t)
            t.refresh_from_db()
            hash_id = (t.extracted or {}).get("care_panel_ticket_id")
            if hash_id:
                self.stdout.write(self.style.SUCCESS(
                    f"  OK    {t.ticket_id}: {t.tracking_url}"))
                restored += 1
            else:
                self.stdout.write(self.style.WARNING(
                    f"  FAIL  {t.ticket_id}: still no Care Panel hash "
                    f"(link={t.tracking_url or '(none)'})"))
                failed += 1

        if not o["dry_run"]:
            self.stdout.write(self.style.SUCCESS(
                f"\nDone. restored={restored} skipped={skipped} failed={failed}"))
