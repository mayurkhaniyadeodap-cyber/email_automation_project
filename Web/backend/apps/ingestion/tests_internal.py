"""
Internal Recipient Workflow: an email sent TO/Cc/Bcc an internal company address NEVER enters
the customer-support pipeline (no ticket, auto-reply, escalation, verification, tracking,
evidence, pending). It is routed to the separate Internal Communications inbox.

    python manage.py test apps.ingestion.tests_internal
"""
from django.test import TestCase, override_settings

from apps.ingestion import service
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Escalation, InternalEmail, PendingConversation, Ticket

INTERNAL = ["grievance.officer@deodap.com", "samir@deodap.com", "ceooffice@deodap.com",
            "nch-ca@gov.in", "samir.rajani@gmail.com"]


@override_settings(INTERNAL_RECIPIENTS=INTERNAL)
class InternalQueueTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.sent = []
        self._oe = service._send_customer_email
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body, **k}) or "<sent>")
        # Classification / escalation must NEVER run for an internal email.
        self._oc = service._classify_dict
        service._classify_dict = lambda b, m: (_ for _ in ()).throw(
            AssertionError("classification ran for an internal email"))

    def tearDown(self):
        service._send_customer_email = self._oe
        service._classify_dict = self._oc

    def _run(self, *, to="grievance.officer@deodap.com", cc="", bcc="", subject="Hello",
             body="some internal note", mid="<i1@x>", blobs=None):
        msg = {"subject": subject, "body_text": body, "from_email": "boss@deodap.com",
               "from_name": "The Boss", "to": to, "cc": cc, "bcc": bcc,
               "message_id": mid, "gmail_message_id": mid}
        if blobs:
            msg["attachment_blobs"] = blobs
        return service.handle_incoming_email(self.mailbox, msg)

    # --- required tests ----------------------------------------------------------------------
    def test_internal_email_skips_ticket_creation(self):
        t, m, obj = self._run()
        self.assertIsNone(t)
        self.assertEqual(Ticket.objects.count(), 0)

    def test_internal_email_skips_auto_reply(self):
        self._run()
        self.assertEqual(self.sent, [])                       # no automatic email at all

    def test_internal_email_skips_escalation(self):
        # Body has a legal keyword that WOULD escalate -- internal routing wins, so NO escalation.
        self._run(body="my lawyer will send a legal notice and consumer court complaint")
        self.assertEqual(Escalation.objects.count(), 0)
        self.assertEqual(InternalEmail.objects.count(), 1)
        self.assertEqual(PendingConversation.objects.count(), 0)

    def test_internal_email_goes_to_internal_queue(self):
        self._run(to="samir@deodap.com")
        ie = InternalEmail.objects.get()
        self.assertEqual(ie.status, InternalEmail.STATUS_INTERNAL_REVIEW)
        self.assertEqual(ie.priority, "normal")
        self.assertEqual(ie.matched_recipient, "samir@deodap.com")
        self.assertEqual(ie.sender, "boss@deodap.com")

    def test_internal_detected_via_cc(self):
        # Internal address only on Cc -> still routed internally.
        self._run(to="random@buyer.com", cc="ceooffice@deodap.com")
        ie = InternalEmail.objects.get()
        self.assertEqual(ie.matched_recipient, "ceooffice@deodap.com")

    def test_non_internal_email_is_not_routed(self):
        # A normal customer email (no internal recipient) must NOT create an internal record.
        # It will try the normal pipeline; classify is stubbed to raise, proving routing didn't fire.
        with self.assertRaises(AssertionError):
            self._run(to="care@deodap.com", cc="", body="where is my order")
        self.assertEqual(InternalEmail.objects.count(), 0)

    def test_internal_email_supports_attachments(self):
        self._run(blobs=[{"filename": "doc.pdf", "content": b"%PDF-1.4 data",
                          "mime_type": "application/pdf"}])
        ie = InternalEmail.objects.get()
        self.assertEqual(len(ie.attachments), 1)
        self.assertEqual(ie.attachments[0]["filename"], "doc.pdf")
        self.assertEqual(ie.email_attachments.count(), 1)

    def test_internal_reply_preserves_thread(self):
        self._run(mid="<orig@x>")
        ie = InternalEmail.objects.get()
        service.send_internal_reply(ie, "Acknowledged.", agent="alice")
        out = next(c for c in ie.conversation if c.get("direction") == "outbound")
        # reply threads on the original message id (In-Reply-To / References)
        self.assertEqual(self.sent[-1]["in_reply_to"], "<orig@x>")
        self.assertIn("<orig@x>", self.sent[-1]["references"])
        self.assertEqual(ie.status, InternalEmail.STATUS_AWAITING_REPLY)


@override_settings(INTERNAL_RECIPIENTS=INTERNAL)
class InternalActionTests(TestCase):
    """Agent actions through the real API: archive + dashboard counts."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.agent = get_user_model().objects.create_superuser("alice", "agent@x.com", "x")
        self.client = APIClient()
        self.client.force_authenticate(self.agent)

    def _ie(self, **kw):
        kw.setdefault("status", InternalEmail.STATUS_INTERNAL_REVIEW)
        return InternalEmail.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            sender="boss@deodap.com", subject="hi", body="note", matched_recipient="samir@deodap.com",
            message_id="<o@x>", thread_ids=["<o@x>"], received_at=None, **kw)

    def test_internal_archive(self):
        ie = self._ie()
        r = self.client.post(f"/api/internal-emails/{ie.id}/archive/", {}, format="json")
        self.assertEqual(r.status_code, 200)
        ie.refresh_from_db()
        self.assertEqual(ie.status, InternalEmail.STATUS_ARCHIVED)

    def test_internal_dashboard_counts(self):
        from apps.analytics import dashboard as dash
        self._ie()
        self._ie(status=InternalEmail.STATUS_ARCHIVED)
        self._ie(status=InternalEmail.STATUS_DELETED)     # deleted excluded from counts
        m = dash.internal_metrics([self.brand.id])
        self.assertEqual(m["total"], 2)                   # deleted excluded
        self.assertEqual(m["pending"], 1)
        self.assertEqual(m["archived"], 1)
        s = dash.summary([self.brand.id])
        self.assertEqual(s["internal"]["total"], 2)
