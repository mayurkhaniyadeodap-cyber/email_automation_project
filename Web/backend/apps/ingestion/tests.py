"""
Offline tests for the Phase 1 ingestion pipeline (doc sections 2 & 3).

None of these need a live Gmail connection: normalize/ignore_gate/ingest operate on
plain dicts, and the webhook test injects a fake Gmail client. Run with:

    python manage.py test apps.ingestion
"""

import base64
import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.brand_settings.models import BlockListEntry
from apps.ingestion import ignore_gate, service
from apps.ingestion.normalize import parse_gmail_message
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Message, Ticket

User = get_user_model()


def b64url(text):
    """Gmail body data is base64url, often without padding."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def gmail_raw(
    *,
    msg_id="m1",
    thread_id="t1",
    from_addr="Buyer <buyer@example.com>",
    to_addr="care@deodap.com",
    subject="Where is my order DD123?",
    text="Where is my order DD123, when will it come?",
    html=None,
    extra_headers=None,
    attachments=None,
):
    """Build a Gmail users.messages.get (format=full) resource for tests."""
    headers = [
        {"name": "From", "value": from_addr},
        {"name": "To", "value": to_addr},
        {"name": "Subject", "value": subject},
        {"name": "Message-ID", "value": f"<{msg_id}@mail.example.com>"},
    ]
    for name, value in (extra_headers or {}).items():
        headers.append({"name": name, "value": value})

    parts = [{"mimeType": "text/plain", "body": {"data": b64url(text)}}]
    if html:
        parts.append({"mimeType": "text/html", "body": {"data": b64url(html)}})
    for att in attachments or []:
        parts.append(
            {
                "filename": att["filename"],
                "mimeType": att.get("mime_type", "application/octet-stream"),
                "body": {"attachmentId": att.get("attachment_id", "att1"),
                         "size": att.get("size", 10)},
            }
        )

    return {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": text[:50],
        "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts},
    }


class FakeGmailClient:
    """Stand-in for GmailClient that serves canned raw messages by id."""

    def __init__(self, raw_messages, latest_history="999"):
        self._by_id = {r["id"]: r for r in raw_messages}
        self._latest = latest_history
        self.sent = []

    def list_recent_message_ids(self, max_results=25):
        return list(self._by_id.keys())

    def list_history(self, start_history_id):
        return list(self._by_id.keys())

    def get_message(self, message_id):
        return self._by_id.get(message_id)

    def latest_history_id(self):
        return self._latest

    def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return "sent-123"


class BaseFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(
            brand=self.brand, email_address="care@deodap.com"
        )


class NormalizeTests(TestCase):
    def test_parses_headers_body_and_attachment(self):
        raw = gmail_raw(
            html="<p>hi</p>",
            extra_headers={"References": "<a@x> <b@x>", "In-Reply-To": "<a@x>"},
            attachments=[{"filename": "proof.jpg", "mime_type": "image/jpeg"}],
        )
        n = parse_gmail_message(raw)
        self.assertEqual(n["gmail_message_id"], "m1")
        self.assertEqual(n["thread_id"], "t1")
        self.assertEqual(n["from_email"], "buyer@example.com")
        self.assertEqual(n["from_name"], "Buyer")
        self.assertEqual(n["to"], "care@deodap.com")
        self.assertIn("DD123", n["body_text"])
        self.assertEqual(n["body_html"], "<p>hi</p>")
        self.assertEqual(n["references"], ["<a@x>", "<b@x>"])
        self.assertEqual(n["in_reply_to"], "<a@x>")
        self.assertEqual(len(n["attachments"]), 1)
        self.assertEqual(n["attachments"][0]["filename"], "proof.jpg")

    def test_nested_multipart_collects_text(self):
        raw = {
            "id": "m2", "threadId": "t2",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [{"name": "From", "value": "x@y.com"}],
                "parts": [
                    {"mimeType": "multipart/alternative", "parts": [
                        {"mimeType": "text/plain", "body": {"data": b64url("hello nested")}},
                    ]},
                ],
            },
        }
        n = parse_gmail_message(raw)
        self.assertEqual(n["body_text"], "hello nested")


class IgnoreGateTests(BaseFixture):
    def _ignore(self, **msg):
        msg.setdefault("from_email", "buyer@example.com")
        msg.setdefault("headers", {})
        return ignore_gate.evaluate(self.brand, msg)

    def test_real_customer_passes(self):
        self.assertFalse(self._ignore(from_email="buyer@example.com"))

    def test_exact_sender_blocked(self):
        BlockListEntry.objects.create(
            brand=self.brand, kind=BlockListEntry.KIND_SENDER, value="spam@bad.com"
        )
        self.assertTrue(self._ignore(from_email="spam@bad.com"))
        self.assertFalse(self._ignore(from_email="good@bad.com"))

    def test_domain_blocked_with_wildcard_and_subdomain(self):
        BlockListEntry.objects.create(
            brand=self.brand, kind=BlockListEntry.KIND_DOMAIN, value="*@newsletter.xyz"
        )
        self.assertTrue(self._ignore(from_email="promo@newsletter.xyz"))
        self.assertTrue(self._ignore(from_email="promo@mail.newsletter.xyz"))
        self.assertFalse(self._ignore(from_email="buyer@example.com"))

    def test_noreply_pattern(self):
        BlockListEntry.objects.create(
            brand=self.brand, kind=BlockListEntry.KIND_NOREPLY, value="noreply@"
        )
        self.assertTrue(self._ignore(from_email="noreply@vendor.com"))
        self.assertFalse(self._ignore(from_email="buyer@vendor.com"))

    def test_internal_domain(self):
        BlockListEntry.objects.create(
            brand=self.brand, kind=BlockListEntry.KIND_INTERNAL, value="@deodap.com"
        )
        self.assertTrue(self._ignore(from_email="staff@deodap.com"))

    def test_marketing_header_present(self):
        BlockListEntry.objects.create(
            brand=self.brand, kind=BlockListEntry.KIND_MARKETING, value="List-Unsubscribe"
        )
        self.assertTrue(
            self._ignore(headers={"List-Unsubscribe": "<mailto:u@x>"})
        )
        self.assertFalse(self._ignore(headers={}))

    def test_marketing_header_name_value(self):
        BlockListEntry.objects.create(
            brand=self.brand, kind=BlockListEntry.KIND_MARKETING, value="Precedence: bulk"
        )
        self.assertTrue(self._ignore(headers={"Precedence": "bulk"}))
        self.assertFalse(self._ignore(headers={"Precedence": "list"}))

    def test_inactive_entry_ignored(self):
        BlockListEntry.objects.create(
            brand=self.brand, kind=BlockListEntry.KIND_SENDER,
            value="spam@bad.com", is_active=False,
        )
        self.assertFalse(self._ignore(from_email="spam@bad.com"))


class IngestMessageTests(BaseFixture):
    def test_new_mail_creates_ticket_and_message(self):
        n = parse_gmail_message(gmail_raw())
        ticket, msg, created = service.ingest_message(self.mailbox, n)
        self.assertTrue(created)
        self.assertEqual(ticket.brand, self.brand)
        self.assertEqual(ticket.organization, self.org)
        self.assertEqual(ticket.thread_id, "t1")
        self.assertEqual(ticket.customer_email, "buyer@example.com")
        self.assertEqual(msg.direction, Message.DIRECTION_INBOUND)
        self.assertFalse(ticket.is_ignored)
        self.assertTrue(ticket.audit_log.filter(event="ticket_created").exists())

    def test_reply_joins_same_thread(self):
        service.ingest_message(self.mailbox, parse_gmail_message(gmail_raw()))
        reply = gmail_raw(msg_id="m2", thread_id="t1", text="any update?")
        ticket, msg, created = service.ingest_message(self.mailbox, parse_gmail_message(reply))
        self.assertTrue(created)
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(ticket.messages.count(), 2)

    def test_dedup_on_gmail_message_id(self):
        n = parse_gmail_message(gmail_raw())
        service.ingest_message(self.mailbox, n)
        ticket, msg, created = service.ingest_message(self.mailbox, n)
        self.assertFalse(created)
        self.assertEqual(Message.objects.count(), 1)

    def test_ignored_mail_flagged_not_in_queue(self):
        BlockListEntry.objects.create(
            brand=self.brand, kind=BlockListEntry.KIND_NOREPLY, value="noreply@"
        )
        n = parse_gmail_message(gmail_raw(from_addr="noreply@vendor.com"))
        ticket, msg, created = service.ingest_message(self.mailbox, n)
        self.assertTrue(ticket.is_ignored)
        self.assertEqual(ticket.status, Ticket.STATUS_IGNORED)
        self.assertTrue(ticket.ignored_reason)
        self.assertTrue(ticket.audit_log.filter(event="ignored").exists())


@override_settings(CLASSIFIER_RULE_FALLBACK=False)
class SyncHistoryTests(BaseFixture):
    def test_sync_history_ingests_and_advances_history_id(self):
        fake = FakeGmailClient(
            [gmail_raw(msg_id="m1", thread_id="t1"),
             gmail_raw(msg_id="m2", thread_id="t2", subject="Refund?")],
            latest_history="555",
        )
        results = service.sync_history(self.mailbox, client=fake)
        self.assertEqual(len(results), 2)
        self.assertEqual(Ticket.objects.count(), 2)
        self.mailbox.refresh_from_db()
        self.assertEqual(self.mailbox.gmail_history_id, "555")

    def test_new_history_id_param_wins(self):
        fake = FakeGmailClient([gmail_raw()], latest_history="555")
        service.sync_history(self.mailbox, new_history_id="777", client=fake)
        self.mailbox.refresh_from_db()
        self.assertEqual(self.mailbox.gmail_history_id, "777")


@override_settings(CLASSIFIER_RULE_FALLBACK=False)
class WebhookTests(BaseFixture):
    def setUp(self):
        super().setUp()
        self.api = APIClient()

    def _envelope(self, email="care@deodap.com", history_id="555"):
        data = base64.b64encode(
            json.dumps({"emailAddress": email, "historyId": history_id}).encode()
        ).decode()
        return {"message": {"data": data, "messageId": "p1"}, "subscription": "s1"}

    def test_webhook_ingests_via_injected_client(self):
        fake = FakeGmailClient([gmail_raw(), gmail_raw(msg_id="m2", thread_id="t2")])

        def fake_build(mailbox):
            return fake

        original = service.build_client
        service.build_client = fake_build
        try:
            resp = self.api.post("/api/gmail/webhook", self._envelope(), format="json")
        finally:
            service.build_client = original

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["ingested"], 2)
        self.assertEqual(Ticket.objects.count(), 2)

    def test_webhook_unknown_mailbox_acks_204(self):
        resp = self.api.post(
            "/api/gmail/webhook", self._envelope(email="nope@nowhere.com"), format="json"
        )
        self.assertEqual(resp.status_code, 204)

    def test_webhook_token_guard(self):
        with self.settings(GMAIL_PUBSUB_TOKEN="secret"):
            resp = self.api.post("/api/gmail/webhook", self._envelope(), format="json")
            self.assertEqual(resp.status_code, 403)
            resp_ok = self.api.post(
                "/api/gmail/webhook?token=secret", self._envelope(), format="json"
            )
            self.assertIn(resp_ok.status_code, (200, 204))
