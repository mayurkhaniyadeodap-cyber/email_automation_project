"""
Run one waiting-state sweep manually: send 24h reminders (M7R) and 72h auto-closes
(M7C). The scheduler runs this automatically every WAITING_SWEEP_MINUTES; this command
is for on-demand runs / cron.

    python manage.py sweep_waiting
"""

from django.core.management.base import BaseCommand

from apps.ingestion import timers


class Command(BaseCommand):
    help = "Send waiting-state reminders (24h) and auto-closes (72h)."

    def handle(self, *args, **opts):
        reminded, closed = timers.sweep_waiting_states()
        self.stdout.write(self.style.SUCCESS(
            f"Sweep done: {reminded} reminder(s) sent, {closed} conversation(s) auto-closed."))
