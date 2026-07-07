"""
Cash on Delivery (COD) inquiry workflow: DeoDap is online-prepaid ONLY. A COD inquiry gets a
FIXED auto-reply, is marked Auto Resolved, creates NO ticket, asks NO pincode, and is never sent
for manual review. Duplicate auto-replies for the same conversation are prevented.

    python manage.py test apps.ingestion.tests_cod
"""
from django.test import TestCase

from apps.decision import policy
from apps.ingestion import service
from apps.ingestion.tests_imap import FakeImap
from apps.ingestion.tests_smart import eml
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import PendingConversation, ProcessedEmail, Ticket


class CodDetectTests(TestCase):
    def test_detects_cod_phrasings(self):
        for t in ["Is COD available?", "Do you offer cash on delivery?",
                  "cash on delivery available for my area?", "can I pay on delivery",
                  "do you have a cash payment option", "I want C.O.D", "COD available or not"]:
            self.assertTrue(policy.cod_inquiry(t), t)

    def test_not_cod(self):
        for t in ["please share the code", "my promo code is not working",
                  "where is my order", "I was charged twice"]:
            self.assertFalse(policy.cod_inquiry(t), t)


class CodFlowTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _run(self, *emails):
        self.sent = []
        orig = service._send_customer_email
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body}) or "<sent>")
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            service._send_customer_email = orig

    # 1) COD inquiry -> the fixed prepaid-only reply.
    def test_sends_prepaid_only_reply(self):
        self._run(eml(subject="COD?", body="Is cash on delivery available?", message_id="<a@x>"))
        self.assertEqual(len(self.sent), 1)
        s = self.sent[0]
        self.assertEqual(s["subject"], "Cash on Delivery (COD) Information")
        b = s["body"].lower()
        self.assertIn("cash on delivery (cod) is not available", b)
        self.assertIn("online prepaid payment", b)
        self.assertIn("regards,\ndeodap support team", b)      # signature appended

    # 2) NO ticket (and no pending / manual review).
    def test_no_ticket_created(self):
        self._run(eml(subject="COD?", body="do you offer cash on delivery?", message_id="<a@x>"))
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertEqual(PendingConversation.objects.count(), 0)

    # 3) NO pincode is requested.
    def test_no_pincode_requested(self):
        self._run(eml(subject="COD?", body="is COD available for pincode 380001?",
                      message_id="<a@x>"))
        b = self.sent[0]["body"].lower()
        self.assertNotIn("pincode", b)
        self.assertNotIn("pin code", b)

    # 4) Conversation marked Auto Resolved (logs + completed ProcessedEmail, no ticket).
    def test_marked_auto_resolved(self):
        with self.assertLogs("apps.ingestion.service", level="INFO") as cm:
            self._run(eml(subject="COD?", body="cash on delivery?", message_id="<a@x>"))
        blob = "\n".join(cm.output)
        self.assertIn("COD_INQUIRY_DETECTED", blob)
        self.assertIn("COD_AUTO_REPLY_SENT", blob)
        self.assertIn("COD_AUTO_RESOLVED", blob)
        pe = ProcessedEmail.objects.get(message_id="<a@x>")
        self.assertTrue(pe.auto_reply_sent)                    # the auto-resolved record
        self.assertIsNotNone(pe.completed_at)
        self.assertEqual(Ticket.objects.count(), 0)

    # 5a) Duplicate prevented: the SAME email re-delivered (re-poll) -> replied only once.
    def test_duplicate_same_email_prevented(self):
        e = eml(subject="COD?", body="is cash on delivery available?", message_id="<a@x>")
        self._run(e, e)
        self.assertEqual(len(self.sent), 1)

    # 5b) Duplicate prevented: a follow-up in the SAME conversation -> one COD reply per thread.
    def test_duplicate_same_conversation_prevented(self):
        self._run(
            eml(subject="COD?", body="is COD available?", message_id="<a@x>"),
            eml(subject="Re: COD?", body="any update on cash on delivery?", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>"),
        )
        self.assertEqual(len(self.sent), 1)
