"""
Evidence upload -> ticket creation -> the confirmation email MUST ALWAYS be sent.

Covers ALL evidence-based categories (they all promote via _promote_pending) and the reported
bug: once the ticket is created, a failure in a best-effort finalize step (Care Panel store /
tracking / media) must NEVER prevent the customer's "Support Ticket Created Successfully" email.

    python manage.py test apps.ingestion.tests_evidence_confirmation
"""
from email.message import EmailMessage

from django.test import TestCase, override_settings

from apps.classifier.service import ClassificationResult
from apps.ingestion import service
from apps.ingestion.tests_imap import FakeImap
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, SubTopic
from apps.tickets.models import Message, PendingConversation, Ticket

CONFIRM_SUBJECT = "Support Ticket Created Successfully"


def eml(*, subject, body, message_id, in_reply_to=None, references=None, image=False, video=False):
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
    if image:
        m.add_attachment(b"\x89PNGdata", maintype="image", subtype="png", filename="photo.png")
    if video:
        m.add_attachment(b"\x00\x00mp4", maintype="video", subtype="mp4", filename="clip.mp4")
    return m.as_bytes()


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class EvidenceConfirmationTests(TestCase):
    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        self.cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        self.sub = SubTopic.objects.create(category=self.cat, code="3.4", name="Wrong Item",
                                            requires_video=True, requires_evidence=True)

    def _classify(self):
        return lambda b, m: ClassificationResult(
            category="3. Delivery Issues", sub_topic="Wrong Item", confidence=0.9, extracted={},
            sentiment="neutral", language="en", is_support_request=True,
            issue_summary="received wrong item", requires_evidence=True, requires_agent=False,
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

    def _emails(self):
        return (
            eml(subject="wrong item", body="I received the wrong item", message_id="<c1@x>"),
            eml(subject="Re: wrong item", body="my order id is 262288292", message_id="<c2@x>",
                in_reply_to="<reply1@deodap.com>", references="<c1@x> <reply1@deodap.com>"),
            eml(subject="Re: wrong item", body="photo and video attached", message_id="<c3@x>",
                in_reply_to="<reply2@deodap.com>", references="<c1@x> <reply2@deodap.com>",
                image=True, video=True),
        )

    def _confirmation(self, ticket):
        return ticket.messages.filter(direction=Message.DIRECTION_OUTBOUND,
                                      subject=CONFIRM_SUBJECT).first()

    def test_evidence_upload_creates_ticket_and_sends_confirmation(self):
        self._run(*self._emails())
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(PendingConversation.objects.count(), 0)        # pending cleared
        t = Ticket.objects.get()
        self.assertIsNotNone(self._confirmation(t), "confirmation email must be sent")
        self.assertTrue(t.audit_log.filter(event="confirmation_sent").exists())

    def test_confirmation_sent_even_if_care_panel_store_raises(self):
        # Inject a failure in a best-effort finalize step -> the confirmation MUST still go out.
        orig = service._store_care_panel
        service._store_care_panel = lambda t: (_ for _ in ()).throw(RuntimeError("store boom"))
        try:
            self._run(*self._emails())
        finally:
            service._store_care_panel = orig
        t = Ticket.objects.get()
        self.assertIsNotNone(self._confirmation(t),
                             "confirmation must be sent even when Care Panel store fails")

    def test_confirmation_sent_even_if_tracking_and_media_raise(self):
        # _ensure_tracking runs in BOTH finalize and send_confirmation prep; _upload_care_panel_media
        # in finalize. Both raising must still produce the confirmation (no-link M5N fallback).
        ot, om = service._ensure_tracking, service._upload_care_panel_media
        service._ensure_tracking = lambda t: (_ for _ in ()).throw(RuntimeError("track boom"))
        service._upload_care_panel_media = lambda t: (_ for _ in ()).throw(RuntimeError("media boom"))
        try:
            self._run(*self._emails())
        finally:
            service._ensure_tracking, service._upload_care_panel_media = ot, om
        t = Ticket.objects.get()
        self.assertIsNotNone(self._confirmation(t),
                             "confirmation must be sent even when tracking/media fail")
