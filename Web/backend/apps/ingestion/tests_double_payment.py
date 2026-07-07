"""
Double Payment / Payment Deducted Twice workflow: NEVER an immediate ticket -- progressively
collect a Registered Mobile Number + a Payment Screenshot, ask ONLY for what is missing, never
repeat the same request, then verify + create the ticket.

    python manage.py test apps.ingestion.tests_double_payment
"""
from django.test import TestCase

from apps.decision import policy
from apps.ingestion import service
from apps.ingestion.tests_imap import FakeImap
from apps.ingestion.tests_smart import eml
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import PendingConversation, Ticket


class DoublePaymentDetectTests(TestCase):
    def test_detects_double_payment_phrasings(self):
        for t in ["I was charged twice for my order", "amount deducted twice from my account",
                  "double payment happened", "i mistakenly made the payment twice",
                  "paid twice for the same order", "duplicate charge on my card",
                  "money got debited two times"]:
            self.assertTrue(policy.double_payment(t), t)

    def test_not_double_payment(self):
        # 'payment deducted but order not placed' is a DIFFERENT concern, not double payment.
        self.assertFalse(policy.double_payment("payment deducted but order not placed"))
        self.assertFalse(policy.double_payment("where is my order"))


class DoublePaymentFlowTests(TestCase):
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

    def _last(self):
        return self.sent[-1]["body"] if self.sent else ""

    # 1) First email, no info -> hold, ask for BOTH, NO ticket.
    def test_first_email_holds_and_asks_both(self):
        self._run(eml(subject="Refund", body="I was charged twice for my order, please help.",
                      message_id="<a@x>"))
        self.assertEqual(Ticket.objects.count(), 0)              # NO immediate ticket
        self.assertEqual(PendingConversation.objects.count(), 1)
        b = self._last()
        self.assertIn("Registered Mobile Number", b)
        self.assertIn("Payment Screenshot", b)

    # 2) Only mobile -> ask ONLY for the screenshot.
    def test_mobile_only_asks_screenshot_only(self):
        self._run(eml(subject="Refund",
                      body="I was charged twice. My registered mobile is 9876543210.",
                      message_id="<a@x>"))
        self.assertEqual(Ticket.objects.count(), 0)
        b = self._last()
        self.assertIn("Payment Screenshot", b)
        self.assertNotIn("Registered Mobile Number", b)         # already have the mobile

    # 3) Only screenshot -> ask ONLY for the mobile.
    def test_screenshot_only_asks_mobile_only(self):
        self._run(eml(subject="Refund", body="I was charged twice, screenshot attached.",
                      message_id="<a@x>", image=True))
        self.assertEqual(Ticket.objects.count(), 0)
        b = self._last()
        self.assertIn("Registered Mobile Number", b)
        self.assertNotIn("Payment Screenshot", b)               # already have the screenshot

    # 4) Both present on the first email -> verify -> ticket immediately, pending consumed.
    def test_both_on_first_email_creates_ticket(self):
        self._run(eml(subject="Refund",
                      body="I was charged twice, my mobile 9876543210, screenshot attached.",
                      message_id="<a@x>", image=True))
        self.assertEqual(Ticket.objects.count(), 1)             # verified (Shopify off) -> ticket
        self.assertEqual(PendingConversation.objects.count(), 0)

    # 5) Progressive: neither -> mobile -> screenshot -> ticket. Each ask is only the missing item.
    def test_progressive_then_ticket(self):
        self._run(
            eml(subject="Refund", body="I was charged twice, please refund.", message_id="<a@x>"),
            eml(subject="Re: Refund", body="My mobile number is 9876543210", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>"),
            eml(subject="Re: Refund", body="Here is the screenshot.", message_id="<a3@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True),
        )
        self.assertEqual(Ticket.objects.count(), 1)             # created only once both received
        self.assertEqual(PendingConversation.objects.count(), 0)
        second = self.sent[1]["body"]                           # the reply after the mobile arrived
        self.assertIn("Payment Screenshot", second)
        self.assertNotIn("Registered Mobile Number", second)   # never re-asks the mobile

    # 6) No ticket (and therefore no 'Ticket Created' email) until BOTH are received.
    def test_no_ticket_until_both(self):
        self._run(eml(subject="Refund", body="charged twice, my mobile is 9876543210",
                      message_id="<a@x>"))
        self.assertEqual(Ticket.objects.count(), 0)             # screenshot still missing

    # 7) Do NOT send the same request repeatedly when nothing new arrives.
    def test_does_not_repeat_same_request(self):
        self._run(
            eml(subject="Refund", body="I was charged twice, screenshot attached.",
                message_id="<a@x>", image=True),                # -> asks mobile (once)
            eml(subject="Re: Refund", body="please help me soon", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>"),        # nothing new -> must NOT re-ask
        )
        mobile_asks = [s for s in self.sent if "Registered Mobile Number" in s["body"]]
        self.assertEqual(len(mobile_asks), 1)                   # asked once, not repeated
        self.assertEqual(Ticket.objects.count(), 0)

    # 8) A re-fetch of the completing reply must NOT create a duplicate ticket.
    def test_no_duplicate_ticket_on_refetch(self):
        first = eml(subject="Refund", body="I was charged twice, please refund.", message_id="<a@x>")
        done = eml(subject="Re: Refund", body="mobile 9876543210, screenshot attached",
                   message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>", image=True)
        self._run(first, done, done)                            # deliver the completing reply TWICE
        self.assertEqual(Ticket.objects.count(), 1)             # exactly one ticket
