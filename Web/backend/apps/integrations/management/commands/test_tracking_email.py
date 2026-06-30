"""
Test the Care Panel tracking-link flow end to end without crafting an email.

It runs the LIVE open-tickets lookup for a phone, shows the matched ticket
(url + ticketNumber), and renders the confirmation email. With --send it actually
emails the rendered message to an address via SMTP.

    python manage.py test_tracking_email --phone 9582872335 --order 262203508
    python manage.py test_tracking_email --phone 9582872335 --send you@example.com
"""

from django.core.management.base import BaseCommand

from apps.ingestion import service
from apps.integrations import care_panel
from apps.organizations.models import Mailbox
from apps.tickets.models import Ticket


class Command(BaseCommand):
    help = "Live-test the Care Panel tracking-link lookup + confirmation email."

    def add_arguments(self, parser):
        parser.add_argument("--phone", required=True)
        parser.add_argument("--order", default="")
        parser.add_argument("--email", default="test-customer@example.com")
        parser.add_argument("--send", default="", help="If set, send the email to this address.")

    def handle(self, *args, **o):
        mb = Mailbox.objects.first()
        if mb is None:
            self.stderr.write("No mailbox configured."); return

        # A throwaway ticket just to drive the lookup; deleted at the end.
        t = Ticket.objects.create(
            organization=mb.brand.organization, brand=mb.brand, mailbox=mb,
            customer_email=o["email"], subject="tracking-email test",
            extracted={"phone": o["phone"], "order_id": o["order"]},
        )
        try:
            self.stdout.write(f"LIVE open-tickets lookup for phone {o['phone']} ...")
            cid = care_panel.sync_ticket(t)
            t.refresh_from_db()
            self.stdout.write(f"  matched care_panel id : {cid or '(none)'}")
            self.stdout.write(f"  tracking_url          : {t.tracking_url or '(none)'}")
            self.stdout.write(f"  ticket_number         : {t.ticket_number or '(none)'}")

            if not t.tracking_url:
                self.stdout.write(self.style.WARNING(
                    "\nNo open Care Panel ticket for this phone -> generic email would send."))
                return

            from apps.ingestion import mails
            number = t.ticket_number or t.ticket_id
            subj, body = mails.render("M6", t.language, ticket_number=number,
                                      tracking_url=t.tracking_url)   # matched existing
            self.stdout.write(self.style.HTTP_INFO("\n--- EMAIL PREVIEW ---"))
            self.stdout.write("Subject: " + subj)
            self.stdout.write(body)

            if o["send"]:
                t.customer_email = o["send"]
                t.save(update_fields=["customer_email"])
                service.send_confirmation(t, "updated")
                self.stdout.write(self.style.SUCCESS(f"\nSent to {o['send']} (check the inbox)."))
        finally:
            t.delete()  # keep the queue clean
