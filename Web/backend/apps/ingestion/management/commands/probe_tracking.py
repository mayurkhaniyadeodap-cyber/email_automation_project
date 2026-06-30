"""Diagnose the tracking-status email for a phone / order against LIVE Shopify.

Shows the RAW Shopify resolution (cancelled_at, fulfillment_status, chosen source, AWB,
tracking_url) and the exact status block the customer would receive -- so a "wrong status"
or "missing link" report can be confirmed without guessing.

    python manage.py probe_tracking --phone 6358956674
    python manage.py probe_tracking --order 262098591
"""

from django.core.management.base import BaseCommand

from apps.ingestion import service
from apps.integrations.context import lookup_tracking
from apps.organizations.models import Mailbox


class Command(BaseCommand):
    help = "Show the RAW Shopify status resolution + rendered tracking email for a phone/order."

    def add_arguments(self, parser):
        parser.add_argument("--phone", default="")
        parser.add_argument("--order", default="")
        parser.add_argument("--email", default="")
        parser.add_argument("--brand", default="", help="Brand name (defaults to the first).")

    def handle(self, *args, **o):
        mb = Mailbox.objects.filter(brand__name=o["brand"]).first() if o["brand"] \
            else Mailbox.objects.first()
        if mb is None:
            self.stderr.write("No mailbox configured.")
            return
        if not (o["phone"] or o["order"] or o["email"]):
            self.stderr.write("Pass --phone and/or --order and/or --email.")
            return

        info = lookup_tracking(mb.brand, order_id=o["order"], phone=o["phone"], email=o["email"])
        self.stdout.write(self.style.HTTP_INFO("--- lookup_tracking result ---"))
        for k in ("configured", "found", "error", "order_id", "customer_name", "customer_phone",
                  "cancelled_at", "cancel_reason", "status_source", "raw_status", "status",
                  "awb", "courier", "tracking_url"):
            self.stdout.write(f"  {k:16}: {info.get(k)!r}")

        # WHY-NO-LINK: the Care Panel store-json API that mints the care.deodap.in ticket link
        # is PHONE-KEYED. No phone (from the customer OR the verified order) -> no link.
        self.stdout.write(self.style.HTTP_INFO("\n--- Care Panel link readiness ---"))
        phone_for_store = o["phone"] or (info.get("customer_phone") or "")
        if not info.get("configured"):
            self.stdout.write(self.style.ERROR(
                "  Shopify NOT configured -> verification falls through, no order phone, "
                "no link."))
        elif not info.get("found"):
            self.stdout.write(self.style.ERROR(
                "  Order/identifier NOT found in Shopify -> no verified phone -> NO LINK."))
        elif phone_for_store:
            self.stdout.write(self.style.SUCCESS(
                f"  Phone available ({phone_for_store}) -> store-json WILL run -> link "
                f"created."))
        else:
            self.stdout.write(self.style.WARNING(
                "  Order verified but it has NO phone on file in Shopify, and the customer "
                "gave none -> store-json is SKIPPED (phone-keyed) -> NO LINK. Add a phone to "
                "the Shopify order, or have the customer include their mobile."))

        self.stdout.write(self.style.HTTP_INFO("\n--- status block the customer would receive ---"))
        if info["found"] and not info["error"]:
            self.stdout.write(service._format_tracking_details(info))
        elif not info["configured"]:
            self.stdout.write("Shopify NOT configured for this brand -> 'tracking unavailable'.")
        elif info["error"]:
            self.stdout.write("Shopify lookup ERRORED -> 'tracking unavailable'.")
        else:
            self.stdout.write("Order NOT found -> 'could not locate your order' (ask again).")

        if info["found"] and not info.get("awb"):
            self.stdout.write(self.style.WARNING(
                "\nNO AWB on this order -> no 'Track Order' link can be built (a fulfilled/"
                "cancelled order with no tracking number has nothing to link to)."))
        if info.get("cancelled_at"):
            self.stdout.write(self.style.SUCCESS(
                f"\ncancelled_at is set -> status forced to 'Cancelled' (source="
                f"{info.get('status_source')})."))
        elif info["found"]:
            self.stdout.write(self.style.WARNING(
                "\ncancelled_at is NULL in Shopify -> the order is NOT cancelled in Shopify; "
                "the RAW status shown is the real Shopify value."))
