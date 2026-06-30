"""
Tests for the Care Panel -> Mail Engine agent-reply webhook (Mail Flow §6 row 4).

    python manage.py test apps.integrations.tests_webhooks
"""

import json

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Message, Ticket

URL = "/api/care-panel/webhook"


@override_settings(CARE_PANEL_WEBHOOK_TOKEN="")  # deterministic regardless of .env
class CarePanelWebhookTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self, **extra):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="buyer@example.com", subject="my order",
            status=Ticket.STATUS_AWAITING_AGENT, ticket_number="2606090601",
            extracted={"care_panel_ticket_id": "gKp64KxaAz"}, **extra)

    def _post(self, body):
        return self.client.post(URL, data=json.dumps(body), content_type="application/json")

    def test_status_mirrored_by_ticket_id(self):
        t = self._ticket()
        r = self._post({"ticket_id": t.ticket_id, "status": "in_progress"})
        self.assertEqual(r.status_code, 200)
        t.refresh_from_db()
        self.assertEqual(t.status, Ticket.STATUS_IN_PROGRESS)
        self.assertTrue(t.audit_log.filter(event="status_mirrored").exists())

    def test_resolved_sets_resolved_at(self):
        t = self._ticket()
        self._post({"ticket_number": "2606090601", "status": "resolved"})
        t.refresh_from_db()
        self.assertEqual(t.status, Ticket.STATUS_RESOLVED)
        self.assertIsNotNone(t.resolved_at)

    def test_agent_message_forwarded_to_customer(self):
        t = self._ticket()
        r = self._post({"hash": "gKp64KxaAz", "agent_message": "We've shipped a replacement."})
        self.assertEqual(r.status_code, 200)
        out = t.messages.filter(direction=Message.DIRECTION_OUTBOUND).last()
        self.assertIsNotNone(out)
        self.assertIn("replacement", out.body_text)
        self.assertTrue(t.audit_log.filter(event="agent_reply_forwarded").exists())

    def test_status_and_reply_together(self):
        t = self._ticket()
        r = self._post({"ticket_id": t.ticket_id, "status": "resolved",
                        "agent_message": "Sorted, thanks!"})
        self.assertEqual(r.json()["applied"], ["status", "reply"])

    def test_unknown_ticket_404(self):
        r = self._post({"ticket_id": "TKT-2026-999999", "status": "resolved"})
        self.assertEqual(r.status_code, 404)

    @override_settings(CARE_PANEL_WEBHOOK_TOKEN="s3cret")
    def test_token_enforced(self):
        t = self._ticket()
        # No token -> forbidden.
        self.assertEqual(self._post({"ticket_id": t.ticket_id, "status": "resolved"}).status_code, 403)
        # Correct header -> ok.
        ok = self.client.post(URL, data=json.dumps({"ticket_id": t.ticket_id, "status": "resolved"}),
                              content_type="application/json", HTTP_X_CARE_PANEL_TOKEN="s3cret")
        self.assertEqual(ok.status_code, 200)

    def test_find_by_ticket_number_with_hash_prefix(self):
        t = self._ticket()
        r = self._post({"ticket_number": "#2606090601", "status": "closed"})
        self.assertEqual(r.status_code, 200)
        t.refresh_from_db()
        self.assertEqual(t.status, Ticket.STATUS_CLOSED)
