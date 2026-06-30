"""Auto-reply SMTP send path: diagnostics + failures must be VISIBLE, never silently
swallowed (the 'customer never received the reply' bug).

    python manage.py test apps.ingestion.tests_smtp_send
"""
import smtplib

from django.test import TestCase, override_settings

from apps.ingestion import smtp_client


class _FakeServer:
    def __init__(self, *a, refused=None, fail_login=False, **k):
        self._refused = refused or {}
        self._fail_login = fail_login
        self.sent = []

    def starttls(self): pass

    def login(self, user, pwd):
        if self._fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        return (235, b"2.7.0 Accepted")

    def send_message(self, msg):
        if self._refused:
            return self._refused
        self.sent.append(msg)
        return {}

    def quit(self): pass


@override_settings(SMTP_HOST="smtp.gmail.com", SMTP_PORT=465, SMTP_USE_SSL=True,
                   IMAP_USER="deodap.4300@gmail.com", IMAP_PASSWORD="app-pass")
class SmtpSendDiagnosticsTests(TestCase):
    def _patch(self, **kw):
        orig = smtplib.SMTP_SSL
        smtplib.SMTP_SSL = lambda *a, **k: _FakeServer(**kw)
        self.addCleanup(lambda: setattr(smtplib, "SMTP_SSL", orig))

    def test_success_returns_message_id_and_logs(self):
        self._patch()
        with self.assertLogs("apps.ingestion.smtp_client", level="INFO") as cm:
            mid = smtp_client.send_email(to="mayurkhaniya.deodap@gmail.com",
                                         subject="Re: missing my product",
                                         body_text="We could not verify...")
        self.assertTrue(mid and mid.startswith("<"))
        log = "\n".join(cm.output)
        self.assertIn("SMTP-SEND-START", log)
        self.assertIn("SMTP-RESPONSE stage=login", log)
        self.assertIn("SMTP-SEND-SUCCESS", log)

    def test_auth_failure_raises_and_logs_failed(self):
        self._patch(fail_login=True)
        with self.assertLogs("apps.ingestion.smtp_client", level="ERROR") as cm:
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                smtp_client.send_email(to="x@y.com", subject="s", body_text="b")
        self.assertIn("SMTP-SEND-FAILED", "\n".join(cm.output))

    def test_recipient_refused_is_not_silent(self):
        self._patch(refused={"x@y.com": (550, b"No such user")})
        with self.assertRaises(smtplib.SMTPRecipientsRefused):
            smtp_client.send_email(to="x@y.com", subject="s", body_text="b")

    @override_settings(SMTP_HOST="", IMAP_USER="", IMAP_PASSWORD="")
    def test_not_configured_raises_loudly(self):
        with self.assertLogs("apps.ingestion.smtp_client", level="ERROR") as cm:
            with self.assertRaises(RuntimeError):
                smtp_client.send_email(to="x@y.com", subject="s", body_text="b")
        self.assertIn("SMTP-SEND-FAILED reason=not_configured", "\n".join(cm.output))


@override_settings(EMAIL_PROVIDER="imap", SMTP_HOST="smtp.gmail.com", SMTP_PORT=465,
                   SMTP_USE_SSL=True, IMAP_USER="deodap.4300@gmail.com", IMAP_PASSWORD="p")
class CustomerEmailWrapperTests(TestCase):
    def test_failure_is_caught_logged_and_returns_none(self):
        from apps.ingestion import service
        orig = smtplib.SMTP_SSL
        smtplib.SMTP_SSL = lambda *a, **k: _FakeServer(fail_login=True)
        self.addCleanup(lambda: setattr(smtplib, "SMTP_SSL", orig))
        with self.assertLogs("apps.ingestion.service", level="INFO") as cm:
            result = service._send_customer_email("mayurkhaniya.deodap@gmail.com",
                                                  "Re: missing my product", "body")
        self.assertIsNone(result)
        log = "\n".join(cm.output)
        self.assertIn("AUTO-REPLY-TO to=mayurkhaniya.deodap@gmail.com", log)
        self.assertIn("SMTP-SEND-FAILED", log)
