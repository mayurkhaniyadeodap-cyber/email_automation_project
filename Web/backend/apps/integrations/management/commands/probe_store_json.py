"""
Print the REAL response from the Care Panel store-json API (Tasks 2-3). Use this to
discover the actual field names before relying on them -- no assumptions.

    python manage.py probe_store_json --token <care.deodap.in token>
    python manage.py probe_store_json --token <token> --auth x-api-key
    python manage.py probe_store_json --token <token> --email a@b.com --phone 9582872335 --order 262203508
"""

import json

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.integrations.care_panel_store import CarePanelStoreClient, extract_tracking


class Command(BaseCommand):
    help = "POST a sample ticket to store-json and print the full response."

    def add_arguments(self, parser):
        parser.add_argument("--token", default=settings.CARE_PANEL_STORE_TOKEN)
        parser.add_argument("--auth", default=settings.CARE_PANEL_STORE_AUTH,
                            choices=["bearer", "x-api-key"])
        parser.add_argument("--url", default=settings.CARE_PANEL_STORE_URL)
        parser.add_argument("--email", default="probe@example.com")
        parser.add_argument("--phone", default="9582872335")
        parser.add_argument("--order", default="262203508")
        parser.add_argument("--subject", default="missing product (probe)")

    def handle(self, *args, **o):
        if not o["token"]:
            self.stderr.write("No token. Pass --token or set CARE_PANEL_STORE_TOKEN in .env.")
            return
        client = CarePanelStoreClient(o["url"], o["token"], o["auth"])
        payload = {
            "name": o["email"].split("@")[0], "email": o["email"],
            "phone": o["phone"], "order_id": o["order"], "subject": o["subject"],
            "message": "probe: inspecting store-json response shape",
        }
        self.stdout.write(f"POST {o['url']}  (auth={o['auth']})")
        self.stdout.write(f"payload: {json.dumps(payload)}\n")
        status, parsed, raw = client.store(payload)

        self.stdout.write(self.style.HTTP_INFO(f"--- STATUS CODE: {status} ---"))
        self.stdout.write("--- RAW BODY ---")
        self.stdout.write(raw[:4000])
        self.stdout.write("\n--- PARSED JSON ---")
        if parsed is None:
            self.stdout.write("(response was not valid JSON)")
            return
        self.stdout.write(json.dumps(parsed, indent=2)[:4000])

        # Tasks 3-4: surface the fields the spec asks about.
        self.stdout.write("\n--- TOP-LEVEL KEYS ---")
        if isinstance(parsed, dict):
            self.stdout.write(", ".join(parsed.keys()))
        tracking_url, ticket_number = extract_tracking(parsed)
        self.stdout.write(self.style.SUCCESS(
            f"\nDETECTED tracking_url = {tracking_url or '(none)'}"))
        self.stdout.write(self.style.SUCCESS(
            f"DETECTED ticket_number = {ticket_number or '(none)'}"))
        for f in ("ticket_id", "ticket_number", "tracking_url", "ticket_url",
                  "public_url", "link", "url"):
            if isinstance(parsed, dict) and f in parsed:
                self.stdout.write(f"  field '{f}': {parsed[f]}")
