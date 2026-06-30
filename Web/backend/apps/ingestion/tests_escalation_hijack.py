"""
Regression: a customer who has an OPEN escalation must NOT have their evidence reply to a
SEPARATE complaint hijacked into that escalation.

Reported bug: "wrong products" evidence reply (photo + video) from a sender who also had an open
"defective" escalation got appended to the escalation by the sender-fallback gate -> the evidence
pending stayed stuck at waiting_for_video and never became a ticket / sent a confirmation.

    python manage.py test apps.ingestion.tests_escalation_hijack
"""
from email.message import EmailMessage

from django.test import TestCase, override_settings

from apps.classifier.service import ClassificationResult
from apps.ingestion import service
from apps.ingestion.tests_imap import FakeImap
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, SubTopic
from apps.tickets.models import Escalation, Message, Ticket


def eml(*, subject, body, message_id, in_reply_to=None, references=None, image=False, video=False):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = "deodap.4123@gmail.com"
    m["To"] = "care@deodap.com"
    m["Message-ID"] = message_id
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if references:
        m["References"] = references
    m.set_content(body)
    if image:
        m.add_attachment(b"\x89PNGdata", maintype="image", subtype="png", filename="photo.png")
    if video:
        m.add_attachment(b"\x00\x00mp4", maintype="video", subtype="mp4", filename="clip.mp4")
    return m.as_bytes()


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class EscalationHijackTests(TestCase):
    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        self.cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        self.sub = SubTopic.objects.create(category=self.cat, code="3.4", name="Wrong Item",
                                            requires_video=True, requires_evidence=True)
        # The customer ALSO has an unrelated OPEN escalation (the hijack source).
        self.open_esc = Escalation.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            sender="deodap.4123@gmail.com", subject="defective", matched_keyword="ceo",
            status=Escalation.STATUS_MANUAL_REVIEW, thread_ids=["<esc-orig@x>"])

    def _classify(self):
        return lambda b, m: ClassificationResult(
            category="3. Delivery Issues", sub_topic="Wrong Item", confidence=0.9, extracted={},
            sentiment="neutral", language="en", is_support_request=True,
            issue_summary="received wrong products", requires_evidence=True, requires_agent=False,
            category_ref=self.cat, sub_topic_ref=self.sub)

    def _run(self, *emails):
        from apps.integrations import context as ctx
        oc, ob, oe = service._classify_dict, ctx.build_clients, service._send_customer_email
        service._classify_dict = self._classify()
        ctx.build_clients = lambda s: {"shopify": None, "shipping": None, "gokwik": None}
        service._send_customer_email = lambda to, subject, body, **k: "<sent>"
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            service._classify_dict, ctx.build_clients, service._send_customer_email = oc, ob, oe

    def test_evidence_reply_not_hijacked_into_open_escalation(self):
        first = eml(subject="wrong products", body="I received the wrong product, order 262302072",
                    message_id="<c1@x>")
        verify = eml(subject="Re: wrong products", body="my order id is 262302072 phone 9723258964",
                     message_id="<c2@x>", in_reply_to="<reply1@deodap.com>",
                     references="<c1@x> <reply1@deodap.com>")
        evidence_reply = eml(subject="Re: wrong products", body="photo and video attached",
                             message_id="<c3@x>", in_reply_to="<reply2@deodap.com>",
                             references="<c1@x> <reply2@deodap.com>", image=True, video=True)
        self._run(first, verify, evidence_reply)

        # The evidence reply created a TICKET with a confirmation -- NOT swallowed into the escalation.
        self.assertEqual(Ticket.objects.count(), 1, "evidence reply must promote to a ticket")
        t = Ticket.objects.get()
        self.assertIsNotNone(
            t.messages.filter(direction=Message.DIRECTION_OUTBOUND,
                              subject="Support Ticket Created Successfully").first(),
            "confirmation must be sent")
        # The unrelated escalation must NOT have absorbed the wrong-products evidence reply.
        self.open_esc.refresh_from_db()
        self.assertNotIn("<c3@x>", self.open_esc.thread_ids)
        self.assertEqual(len(self.open_esc.conversation), 0)

    def test_genuine_escalation_reply_still_appends_by_sender(self):
        # A reply from the sender that does NOT thread into any pending/ticket still continues their
        # open escalation (the sender-fallback behaviour is preserved for genuine cases).
        reply = eml(subject="Re: defective", body="any update on my escalation?",
                    message_id="<c9@x>", in_reply_to="<unknown@x>")
        self._run(reply)
        self.open_esc.refresh_from_db()
        self.assertIn("<c9@x>", self.open_esc.thread_ids)
