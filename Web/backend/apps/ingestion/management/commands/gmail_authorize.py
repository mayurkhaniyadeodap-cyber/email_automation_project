"""
Run the OAuth installed-app flow for a mailbox and store the credentials on
Mailbox.oauth_payload (doc section 2, OAuth scope gmail.modify).

Usage:
    python manage.py gmail_authorize --mailbox care@deodap.com \
        --client-secrets path/to/client_secret.json

Requires google-auth-oauthlib. This opens a browser / prints a URL for consent.
"""

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.ingestion.gmail_client import GMAIL_SCOPE
from apps.organizations.models import Mailbox


class Command(BaseCommand):
    help = "Authorize a Gmail mailbox via OAuth and store its tokens."

    def add_arguments(self, parser):
        parser.add_argument("--mailbox", required=True, help="Mailbox email address.")
        parser.add_argument(
            "--client-secrets",
            required=True,
            help="Path to the Google OAuth client_secret.json.",
        )
        parser.add_argument(
            "--no-browser",
            action="store_true",
            help="Use the console flow (print URL) instead of opening a browser.",
        )

    def handle(self, *args, **opts):
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise CommandError(
                "google-auth-oauthlib is not installed. "
                "Run: pip install -r requirements.txt"
            ) from exc

        mailbox = Mailbox.objects.filter(email_address=opts["mailbox"]).first()
        if not mailbox:
            raise CommandError(f"No mailbox {opts['mailbox']}. Add it first.")

        flow = InstalledAppFlow.from_client_secrets_file(
            opts["client_secrets"], scopes=[GMAIL_SCOPE]
        )
        if opts["no_browser"]:
            creds = flow.run_console()
        else:
            creds = flow.run_local_server(port=0)

        with open(opts["client_secrets"]) as fh:
            secrets = json.load(fh)
        installed = secrets.get("installed") or secrets.get("web") or {}

        mailbox.oauth_payload = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id or installed.get("client_id")
            or settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": creds.client_secret or installed.get("client_secret")
            or settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "scopes": list(creds.scopes or [GMAIL_SCOPE]),
        }
        mailbox.save(update_fields=["oauth_payload", "updated_at"])
        self.stdout.write(
            self.style.SUCCESS(f"Stored OAuth tokens for {mailbox.email_address}.")
        )
        self.stdout.write("Next: python manage.py gmail_watch")
