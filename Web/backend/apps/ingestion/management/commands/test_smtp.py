"""
Verify the outbound (SMTP) reply path end-to-end. Shows the resolved config, performs a
real connect + login (proves the Gmail app password works), and -- if you pass --to --
sends one test email so you can confirm the customer actually receives auto-replies.

Usage:
    python manage.py test_smtp                         # config + connect + login only
    python manage.py test_smtp --to you@example.com    # also SEND a real test email
"""

import smtplib

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Diagnose the SMTP reply path (config + login, optional real send)."

    def add_arguments(self, parser):
        parser.add_argument("--to", help="Send a real test email to this address.")

    def handle(self, *args, **opts):
        w = self.stdout.write
        host = settings.SMTP_HOST
        port = settings.SMTP_PORT
        ssl = settings.SMTP_USE_SSL
        user = settings.IMAP_USER
        pw = settings.IMAP_PASSWORD
        sender = getattr(settings, "REPLY_FROM", "") or user

        w("\n=== SMTP CONFIG (as the running process sees it) ===")
        w(f"  EMAIL_PROVIDER : {getattr(settings, 'EMAIL_PROVIDER', 'imap')}")
        w(f"  SMTP_HOST      : {host}")
        w(f"  SMTP_PORT      : {port}")
        w(f"  SMTP_USE_SSL   : {ssl}")
        w(f"  IMAP_USER      : {user}")
        w(f"  password       : {len(pw)} chars, contains_space={' ' in pw}")
        w(f"  From (sender)  : {sender}")

        if not (host and user and pw):
            w(self.style.ERROR("\nSMTP not configured (SMTP_HOST / IMAP_USER / IMAP_PASSWORD "
                               "missing). Replies cannot send."))
            return
        if " " in pw:
            w(self.style.ERROR("\npassword contains spaces -> Gmail will reject auth (535). "
                               "Use the 16-char app password with NO spaces."))

        # 1) Connect + login -- proves credentials work WITHOUT sending anything.
        w("\n=== CONNECT + LOGIN ===")
        try:
            server = (smtplib.SMTP_SSL(host, port, timeout=15) if ssl
                      else smtplib.SMTP(host, port, timeout=15))
            if not ssl:
                server.starttls()
        except Exception as exc:  # noqa: BLE001
            w(self.style.ERROR(f"  CONNECT FAILED: {exc!r}"))
            w("  -> host/port unreachable or SSL mismatch (Gmail = 465 SSL, or 587 STARTTLS).")
            return
        try:
            code, resp = server.login(user, pw)
            w(self.style.SUCCESS(f"  LOGIN OK: {code} {resp.decode(errors='replace')}"))
        except smtplib.SMTPAuthenticationError as exc:
            w(self.style.ERROR(f"  LOGIN FAILED (auth): {exc.smtp_code} "
                               f"{exc.smtp_error.decode(errors='replace')}"))
            w("  -> wrong/expired app password. Regenerate at Google Account -> Security -> "
              "App passwords, paste the 16 chars (no spaces).")
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
            return
        except Exception as exc:  # noqa: BLE001
            w(self.style.ERROR(f"  LOGIN FAILED: {exc!r}"))
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
            return

        # 2) Optional real send.
        to = opts.get("to")
        if not to:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
            w(self.style.SUCCESS("\nAuth works. Re-run with --to <address> to send a real "
                                 "test email."))
            return

        w(f"\n=== SENDING TEST EMAIL to {to} ===")
        try:
            from apps.ingestion.smtp_client import send_email

            try:
                server.quit()  # close this probe connection; send_email opens its own
            except Exception:  # noqa: BLE001
                pass
            mid = send_email(
                to=to, subject="DeoDap Care - SMTP test",
                body_text="This is a test of the DeoDap Care auto-reply sender. "
                          "If you received this, outbound email is working.")
            w(self.style.SUCCESS(f"  SENT OK. Message-ID: {mid}"))
            w(f"  Check the inbox of {to} (and the Spam folder).")
        except Exception as exc:  # noqa: BLE001
            w(self.style.ERROR(f"  SEND FAILED: {exc!r}"))
            w("  -> see the SMTP-SEND-FAILED log line for the exact stage/code.")
