"""
Second-email (reply-to-pending) auto-reply behaviour + diagnostic logging.

Root cause of "second email gets no auto-reply": the escalation / internal / duplicate
gates run BEFORE the pending-reply path, and a frustrated FOLLOW-UP to an existing
pending can trip an escalation keyword -> diverted to manual review with no auto-reply.
Every such silent exit now logs a REPLY-DECISION line with the exact reason.

    python manage.py test apps.ingestion.tests_reply_decision
"""
from email.message import EmailMessage

from django.test import TestCase, override_settings

from apps.classifier.service import ClassificationResult
from apps.ingestion import service
from apps.ingestion.tests_imap import FakeImap
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, SubTopic
from apps.tickets.models import Escalation, PendingConversation


def eml(*, subject, body, message_id, in_reply_to=None, references=None):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = "buyer@example.com"
    m["To"] = "care@deodap.com"
    m["Message-ID"] = message_id
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if references:
        m["References"] = references
    m.set_content(body)
    return m.as_bytes()


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class ReplyDecisionTests(TestCase):
    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        self.cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        self.sub = SubTopic.objects.create(category=self.cat, code="3.3", name="Damaged",
                                            requires_evidence=True)

    def _classify(self):
        return lambda b, m: ClassificationResult(
            category="3. Delivery Issues", sub_topic="Damaged", confidence=0.9, extracted={},
            sentiment="neutral", language="en", is_support_request=True,
            issue_summary="damaged item", requires_evidence=True, requires_agent=False,
            category_ref=self.cat, sub_topic_ref=self.sub)

    def _run(self, *emails):
        from apps.integrations import context as ctx
        self.sent = []
        oc, ob, oe = service._classify_dict, ctx.build_clients, service._send_customer_email
        service._classify_dict = self._classify()
        ctx.build_clients = lambda s: {"shopify": None, "shipping": None, "gokwik": None}

        def fake_send(to, subject, body, **k):
            sid = f"<reply{len(self.sent) + 1}@deodap.com>"
            self.sent.append({"to": to, "subject": subject, "sid": sid})
            return sid
        service._send_customer_email = fake_send
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            service._classify_dict, ctx.build_clients, service._send_customer_email = oc, ob, oe

    def test_normal_followup_reply_gets_auto_reply(self):
        """A plain reply to a pending (no escalation keyword) DOES get an auto-reply -- the
        workflow does NOT silently stop."""
        first = eml(subject="damaged product", body="my item arrived broken",
                    message_id="<cust1@gmail.com>")
        second = eml(subject="Re: damaged product", body="here is my order id 262288292",
                     message_id="<cust2@gmail.com>", in_reply_to="<reply1@deodap.com>",
                     references="<cust1@gmail.com> <reply1@deodap.com>")
        self._run(first, second)
        # Reply 1 = verify/identifier ask; Reply 2 = evidence (photo) request after the order id.
        self.assertEqual(len(self.sent), 2, f"expected an auto-reply to the 2nd email, got {self.sent}")
        self.assertEqual(PendingConversation.objects.count(), 1)   # same conversation, no duplicate

    def test_escalation_followup_is_diverted_and_logged(self):
        """A frustrated follow-up containing an escalation keyword is diverted to manual review
        with NO auto-reply -- and the exact reason is logged (no silent stop)."""
        first = eml(subject="damaged product", body="my item arrived broken",
                    message_id="<cust1@gmail.com>")
        second = eml(subject="Re: damaged product",
                     body="This is unacceptable. I will file a case in the CONSUMER COURT.",
                     message_id="<cust2@gmail.com>", in_reply_to="<reply1@deodap.com>",
                     references="<cust1@gmail.com> <reply1@deodap.com>")
        with self.assertLogs("apps.ingestion.service", level="INFO") as cm:
            self._run(first, second)
        # The first email got its auto-reply; the escalation follow-up did NOT.
        self.assertEqual(len(self.sent), 1, f"escalation follow-up must not auto-reply: {self.sent}")
        self.assertEqual(Escalation.objects.count(), 1)
        # The exact reason is logged, including that it diverted a reply to the pending.
        joined = "\n".join(cm.output)
        self.assertIn("auto_reply=SKIPPED", joined)
        self.assertIn("reason=escalation_manual_review", joined)
        self.assertIn("REPLY to pending=", joined)
