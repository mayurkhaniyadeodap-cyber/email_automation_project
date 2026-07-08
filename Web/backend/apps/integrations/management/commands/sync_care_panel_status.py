"""Poll the Care Panel and mirror agent-set ticket status into the Mail Engine.

Run by cron (e.g. every 10 minutes). Reuses the existing Care Panel open-tickets API; reconciles
each active local ticket's status with the panel (source of truth). Idempotent and logged.

    python manage.py sync_care_panel_status [--grace-minutes 10]
"""
from django.core.management.base import BaseCommand

from apps.integrations import care_panel_status


class Command(BaseCommand):
    help = "Mirror Care Panel (care.deodap.in) ticket status changes into the Mail Engine."

    def add_arguments(self, parser):
        parser.add_argument(
            "--grace-minutes", type=int, default=10,
            help="Skip tickets created within this many minutes (avoid racing creation).")

    def handle(self, *args, **opts):
        checked, updated, closed = care_panel_status.sync_statuses_from_care_panel(
            grace_minutes=opts["grace_minutes"])
        self.stdout.write(self.style.SUCCESS(
            f"Care Panel status sync: checked={checked} updated={updated} closed={closed}"))
