"""
Make every stored tracking_url resolvable on care.deodap.in:

  * Ticket WITH a real Care Panel hash (extracted.care_panel_ticket_id, from store-json
    data.hash)  ->  https://care.deodap.in/t?id=<careHash>
  * Ticket WITHOUT a real hash (internal Django hash only)  ->  CLEARED ("") because
    care.deodap.in cannot resolve an internal hash (it 404s). Such a ticket then uses the
    no-link confirmation variant instead of a broken link.

After this runs, no stored tracking_url can be a care.deodap.in/<internal-hash> 404 or a
localhost/LAN link.

    python manage.py fix_tracking_urls            # rewrite them
    python manage.py fix_tracking_urls --dry-run  # show what would change
"""

from django.core.management.base import BaseCommand

from apps.ingestion import service
from apps.tickets.models import Ticket


class Command(BaseCommand):
    help = "Set tracking URLs to care.deodap.in/<realHash>, or clear internal-hash 404 links."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        changed = real = cleared = 0
        for t in Ticket.objects.all():
            care_hash = (t.extracted or {}).get("care_panel_ticket_id")
            new_url = service.build_tracking_url(t) if care_hash else ""
            if (t.tracking_url or "") == new_url:
                continue
            kind = "REAL" if new_url else "CLEARED (no care hash)"
            self.stdout.write(f"  {t.ticket_id}: {t.tracking_url or '(none)'}  ->  "
                              f"{new_url or '(none)'}   [{kind}]")
            changed += 1
            real += 1 if new_url else 0
            cleared += 0 if new_url else 1
            if not opts["dry_run"]:
                t.tracking_url = new_url
                t.save(update_fields=["tracking_url", "updated_at"])

        # Safety assertions.
        bad_local = [t.ticket_id for t in Ticket.objects.exclude(tracking_url="")
                     if service._is_local_base(t.tracking_url)]
        bad_internal = [t.ticket_id for t in Ticket.objects.exclude(tracking_url="")
                        if service._is_bad_internal_link(t)]
        verb = "would change" if opts["dry_run"] else "changed"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {changed} ticket(s): {real} real care.deodap.in link(s), "
            f"{cleared} cleared internal-hash link(s)."))
        if bad_local or bad_internal:
            self.stdout.write(self.style.ERROR(
                f"STILL BAD -> local:{bad_local} internal-on-care:{bad_internal}"))
        else:
            self.stdout.write(self.style.SUCCESS(
                "Verified: 0 localhost/LAN and 0 care.deodap.in/<internal-hash> URLs remain."))
