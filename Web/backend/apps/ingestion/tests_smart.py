"""
Tests for Smart Ticket Management: confirmations, audit events, reply-as-update,
evidence-on-attachment, and never creating duplicate tickets.

    python manage.py test apps.ingestion.tests_smart
"""

from email.message import EmailMessage

from django.test import TestCase, override_settings

from apps.ingestion import service
from apps.ingestion.tests_imap import FakeImap
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Attachment, Message, Ticket


def eml(*, subject, body, message_id, from_addr="buyer@example.com",
        in_reply_to=None, references=None, image=False, video=False):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = from_addr
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


class BaseFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")


class DeriveSubjectTests(TestCase):
    """No-subject email -> the ticket subject is the first meaningful body line, else 'No Subject'.
    Covers the 5 reported scenarios (payment / damaged / app / fraud / tracking)."""

    def _subj(self, subject, body):
        return service._derive_subject({"subject": subject, "body_text": body})

    def test_existing_subject_kept(self):
        self.assertEqual(self._subj("Order issue", "body here"), "Order issue")

    def test_blank_subject_payment(self):
        self.assertEqual(self._subj("", "Customer claims to have made a payment but the order "
                                        "was not placed.\nmobile number : 8078518087"),
                         "Customer claims to have made a payment but the order was not placed.")

    def test_blank_subject_damaged(self):
        self.assertEqual(self._subj("", "When i received order this damage so i want to return."),
                         "When i received order this damage so i want to return.")

    def test_blank_subject_app_crashing(self):
        self.assertEqual(self._subj("", "App crashing after login"), "App crashing after login")

    def test_blank_subject_fraud(self):
        self.assertEqual(self._subj("", "I got a fraud call asking for OTP"),
                         "I got a fraud call asking for OTP")

    def test_blank_subject_shipment_tracking(self):
        self.assertEqual(self._subj("", "where is my order my mobile number is 9895798462"),
                         "where is my order my mobile number is 9895798462")

    def test_skips_quoted_and_reply_headers(self):
        body = "> previous quoted text\nOn Wed, Jun 24 wrote:\nWhere is my order?"
        self.assertEqual(self._subj("", body), "Where is my order?")

    def test_no_meaningful_content_is_no_subject(self):
        self.assertEqual(self._subj("", "   \n\n> quoted only\n"), "No Subject")
        self.assertEqual(self._subj(None, ""), "No Subject")

    def test_long_line_truncated(self):
        self.assertEqual(len(self._subj("", "x" * 300)), 120)


class ConfirmationTests(BaseFixture):
    def test_new_ticket_sends_created_confirmation(self):
        # A shipment query needs no evidence -> ticket is created immediately.
        service.fetch_imap(self.mailbox, client=FakeImap([
            eml(subject="Where is my order DD9999?", body="track please", message_id="<a@x>")]))
        t = Ticket.objects.get()
        out = t.messages.filter(direction=Message.DIRECTION_OUTBOUND,
                                subject="Support Ticket Created Successfully").first()
        self.assertIsNotNone(out)
        self.assertIn(t.ticket_id, out.body_text)
        self.assertTrue(t.audit_log.filter(event="ticket_created").exists())
        self.assertTrue(t.audit_log.filter(event="confirmation_sent").exists())

    @override_settings(SEND_TICKET_CONFIRMATIONS=False)
    def test_confirmation_toggle_off(self):
        service.fetch_imap(self.mailbox, client=FakeImap([
            eml(subject="hi", body="help", message_id="<a@x>")]))
        t = Ticket.objects.get()
        self.assertFalse(t.messages.filter(subject="Support Ticket Created Successfully").exists())


class ReplyUpdateTests(BaseFixture):
    def test_reply_updates_same_ticket_no_duplicate(self):
        first = eml(subject="Where is my order?", body="DD9999?", message_id="<a@x>")
        service.fetch_imap(self.mailbox, client=FakeImap([first]))
        reply = eml(subject="Re: Where is my order?", body="any update?",
                    message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>")
        service.fetch_imap(self.mailbox, client=FakeImap([reply], start_uid=2))

        self.assertEqual(Ticket.objects.count(), 1)  # NO duplicate ticket
        t = Ticket.objects.get()
        self.assertEqual(t.messages.filter(direction=Message.DIRECTION_INBOUND).count(), 2)
        self.assertTrue(t.audit_log.filter(event="ticket_updated").exists())
        # An "updated" confirmation went out (M6, with the tracking link now present).
        self.assertTrue(t.messages.filter(
            subject__in=["Existing Ticket Found", "Ticket Updated Successfully"]).exists())

    def test_photo_reply_records_evidence_and_updates(self):
        first = eml(subject="damaged product", body="broken order DD9999", message_id="<a@x>")
        service.fetch_imap(self.mailbox, client=FakeImap([first]))
        photo = eml(subject="Re: damaged product", body="here is the photo, my phone 9876543210",
                    message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>", image=True, video=True)
        service.fetch_imap(self.mailbox, client=FakeImap([photo], start_uid=2))

        t = Ticket.objects.get()
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Attachment.objects.filter(ticket=t).count(), 2)
        self.assertTrue(t.audit_log.filter(event="attachment_received").exists())
        self.assertTrue(t.audit_log.filter(event="evidence_received").exists())
        t.refresh_from_db()
        self.assertTrue(t.extracted.get("has_photo"))
        self.assertTrue(t.extracted.get("has_unboxing_video"))


class EvidenceDeferralTests(BaseFixture):
    """Evidence-required emails defer ticket creation until evidence arrives."""

    def setUp(self):
        super().setUp()
        from apps.brand_settings.models import BrandSettings
        from apps.taxonomy.models import Category, SubTopic
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        SubTopic.objects.create(category=cat, code="3.3", name="Damaged")

    def _fake_provider(self):
        import json as _json

        class FP:
            def generate(self, system, user):
                return _json.dumps({
                    "is_support_request": True,
                    "category": "3. Delivery Issues", "sub_topic": "3.3 Damaged",
                    "confidence": 0.9, "requires_evidence": True, "requires_agent": False,
                    "issue_summary": "damaged product", "sentiment": "frustrated",
                    "extracted": {"order_id": "DD9999"},
                })
        return FP()

    def test_evidence_required_defers_creation(self):
        from apps.classifier import service as classifier
        provider = self._fake_provider()
        orig = classifier.build_provider
        classifier.build_provider = lambda s: provider
        try:
            service.fetch_imap(self.mailbox, client=FakeImap([
                eml(subject="My product is damaged", body="it broke", message_id="<a@x>")]))
        finally:
            classifier.build_provider = orig

        from apps.tickets.models import PendingConversation
        # NO ticket and NO ticket id before evidence -- only a pending conversation.
        self.assertEqual(Ticket.objects.count(), 0)
        p = PendingConversation.objects.get()
        self.assertEqual(p.customer_email, "buyer@example.com")
        self.assertEqual(p.order_id, "DD9999")
        self.assertEqual(p.category, "3. Delivery Issues")
        self.assertGreaterEqual(p.evidence_requests, 1)            # evidence asked for

    def test_evidence_reply_finalizes_ticket(self):
        from apps.classifier import service as classifier
        provider = self._fake_provider()
        orig = classifier.build_provider
        classifier.build_provider = lambda s: provider
        try:
            service.fetch_imap(self.mailbox, client=FakeImap([
                eml(subject="My product is damaged", body="it broke", message_id="<a@x>")]))
            # Customer replies with photo + video evidence.
            service.fetch_imap(self.mailbox, client=FakeImap([
                eml(subject="Re: My product is damaged", body="here is proof, phone 9876543210",
                    message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>",
                    image=True, video=True)], start_uid=2))
        finally:
            classifier.build_provider = orig

        self.assertEqual(Ticket.objects.count(), 1)               # no duplicate
        t = Ticket.objects.get()
        t.refresh_from_db()
        self.assertFalse(t.pending_evidence)                      # finalized
        self.assertTrue(t.audit_log.filter(event="evidence_received").exists())
        self.assertTrue(t.audit_log.filter(event="internal_note").exists())
        # NOW the "created" confirmation is sent.
        self.assertTrue(t.messages.filter(subject="Support Ticket Created Successfully").exists())


class SubTopicEvidenceFlagTests(BaseFixture):
    """A sub-topic flagged requires_evidence defers even when the AI says False
    (the 'missing product' -> 3.1 case)."""

    def setUp(self):
        super().setUp()
        from apps.brand_settings.models import BrandSettings
        from apps.taxonomy.models import Category, SubTopic
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        SubTopic.objects.create(category=cat, code="3.1", name="Not Received",
                                requires_evidence=True)

    def test_ai_says_no_evidence_but_subtopic_flag_defers(self):
        import json as _json
        from apps.classifier import service as classifier

        class FP:
            def generate(self, system, user):
                return _json.dumps({
                    "is_support_request": True, "category": "3. Delivery Issues",
                    "sub_topic": "3.1 Not Received", "confidence": 0.9,
                    "requires_evidence": False,  # AI says no evidence needed
                    "requires_agent": True, "issue_summary": "missing product",
                    "sentiment": "neutral", "extracted": {"order_id": "262203508",
                    "phone": "9582872335"},
                })
        orig = classifier.build_provider
        classifier.build_provider = lambda s: FP()
        try:
            service.fetch_imap(self.mailbox, client=FakeImap([
                eml(subject="missing product", body="my order product is missing",
                    message_id="<a@x>")]))
        finally:
            classifier.build_provider = orig

        from apps.tickets.models import PendingConversation
        self.assertEqual(Ticket.objects.count(), 0)          # NO ticket created
        self.assertEqual(PendingConversation.objects.count(), 1)
        self.assertEqual(PendingConversation.objects.get().order_id, "262203508")


@override_settings(PUBLIC_BASE_URL="https://support.deodap.in")  # portal configured
class OrderIdValidationTests(BaseFixture):
    """New rule: order id / phone do NOT block ticket creation. Once evidence is in and
    we have ANY identifier (email is always present), the ticket is created."""

    def setUp(self):
        super().setUp()
        from apps.brand_settings.models import BrandSettings
        from apps.taxonomy.models import Category, SubTopic
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        SubTopic.objects.create(category=cat, code="3.3", name="Damaged",
                                requires_evidence=True, mandatory_inputs=["order_id"])

    def _provider(self):
        import json as _json

        class FP:
            def generate(self, system, user):
                return _json.dumps({
                    "is_support_request": True, "category": "3. Delivery Issues",
                    "sub_topic": "3.3 Damaged", "confidence": 0.9,
                    "requires_evidence": True, "requires_agent": False,
                    "issue_summary": "damaged", "sentiment": "neutral", "extracted": {}})
        return FP()

    def _run(self, emails):
        from apps.classifier import service as classifier
        orig = classifier.build_provider
        classifier.build_provider = lambda s: self._provider()
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            classifier.build_provider = orig

    def test_photo_without_order_id_still_creates_ticket(self):
        # Damaged + photo, NO order id, NO phone -> email identifier is enough.
        self._run([
            eml(subject="damaged", body="it broke", message_id="<a@x>"),
            # Damaged requires BOTH a photo AND a video -> supply both (still NO order id/phone).
            eml(subject="Re: damaged", body="here is the photo and video", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True, video=True)])
        self.assertEqual(Ticket.objects.count(), 1)              # created on the evidence
        t = Ticket.objects.get()
        self.assertNotIn("care.deodap.in", t.tracking_url or "")  # no internal-hash 404 link
        self.assertTrue(t.attachments.filter(content_type__startswith="image/").exists())

    def test_order_id_is_captured_when_provided(self):
        self._run([
            eml(subject="damaged", body="it broke", message_id="<a@x>"),
            # Damaged requires BOTH a photo AND a video -> supply both.
            eml(subject="Re: damaged", body="photo and video, order id DD9999",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>",
                image=True, video=True)])
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Ticket.objects.get().extracted.get("order_id"), "DD9999")


class VideoMandatoryTests(BaseFixture):
    """Defective/Missing/Wrong Item require a VIDEO; image-only is insufficient."""

    def setUp(self):
        super().setUp()
        from apps.brand_settings.models import BrandSettings
        from apps.taxonomy.models import Category, SubTopic
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        SubTopic.objects.create(category=cat, code="3.3", name="Damaged",
                                requires_evidence=True, requires_video=True,
                                mandatory_inputs=["order_id"])

    def _provider(self):
        import json as _json

        class FP:
            def generate(self, system, user):
                return _json.dumps({
                    "is_support_request": True, "category": "3. Delivery Issues",
                    "sub_topic": "3.3 Damaged", "confidence": 0.9,
                    "requires_evidence": True, "requires_agent": False,
                    "issue_summary": "defective item", "sentiment": "neutral",
                    "extracted": {"order_id": "DD9999"}})
        return FP()

    def _run(self, emails):
        from apps.classifier import service as classifier
        orig = classifier.build_provider
        classifier.build_provider = lambda s: self._provider()
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            classifier.build_provider = orig

    def test_photo_only_is_rejected_waits_for_video(self):
        from apps.tickets.models import PendingConversation
        self._run([
            eml(subject="defective item", body="broken order DD9999", message_id="<a@x>"),
            # reply with a PHOTO only -> NOT enough for a video-mandatory category
            eml(subject="Re: defective", body="photo", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True),
        ])
        self.assertEqual(Ticket.objects.count(), 0)            # NO ticket from a photo
        p = PendingConversation.objects.get()
        self.assertEqual(p.status, "waiting_for_video")
        self.assertGreaterEqual(p.evidence_requests, 2)        # asked for video again
        # The reply continues the SAME thread (In-Reply-To the original message).
        self.assertEqual(p.original_message_id, "<a@x>")
        self.assertIn("unboxing video", service.VIDEO_REQUEST_BODY)
        self.assertIn("mandatory", service.VIDEO_REQUEST_BODY)

    def test_video_creates_ticket(self):
        self._run([
            eml(subject="defective item", body="broken order DD9999", message_id="<a@x>"),
            # Defective requires BOTH a photo AND a video -> supply both.
            eml(subject="Re: defective", body="photo and video, phone 9876543210",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>",
                image=True, video=True),
        ])
        self.assertEqual(Ticket.objects.count(), 1)            # photo + video -> created
        self.assertTrue(Ticket.objects.get().attachments.filter(
            content_type__startswith="video/").exists())


class CategoryVideoGateTests(BaseFixture):
    """Regression for the bug: Defective Product -> category 7, sub_topic_ref=None,
    image-only reply was creating a ticket. Now the CATEGORY flag gates it."""

    def setUp(self):
        super().setUp()
        from apps.brand_settings.models import BrandSettings
        from apps.taxonomy.models import Category
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        # Category 7 flagged requires_video, NO sub-topic created (mirrors real bug).
        Category.objects.create(brand=self.brand, code="7",
                                name="Return, Refund & Replacement", requires_video=True)

    def _provider(self):
        import json as _json

        class FP:
            def generate(self, system, user):
                return _json.dumps({
                    "is_support_request": True,
                    "category": "7. Return, Refund & Replacement", "sub_topic": "",
                    "confidence": 0.9, "requires_evidence": True, "requires_agent": False,
                    "issue_summary": "defective product", "sentiment": "neutral",
                    "extracted": {"order_id": "DD9999"}})
        return FP()

    def _run(self, emails):
        from apps.classifier import service as classifier
        orig = classifier.build_provider
        classifier.build_provider = lambda s: self._provider()
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            classifier.build_provider = orig

    def test_image_only_no_ticket_even_without_subtopic(self):
        from apps.tickets.models import PendingConversation
        self._run([
            eml(subject="Defective Product Received", body="broken DD9999", message_id="<a@x>"),
            eml(subject="Re: Defective", body="photo", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True),
        ])
        self.assertEqual(Ticket.objects.count(), 0)            # BUG FIX: no ticket from image
        p = PendingConversation.objects.get()
        self.assertIsNone(p.sub_topic_ref)                    # no sub-topic, gated by category
        self.assertEqual(p.status, "waiting_for_video")

    def test_video_creates_ticket(self):
        self._run([
            eml(subject="Defective Product Received", body="broken DD9999", message_id="<a@x>"),
            # Defective requires BOTH a photo AND a video -> supply both.
            eml(subject="Re: Defective", body="photo and video, phone 9876543210",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>",
                image=True, video=True),
        ])
        self.assertEqual(Ticket.objects.count(), 1)            # photo + video -> created


@override_settings(PUBLIC_BASE_URL="https://support.deodap.in")  # portal configured
class EvidenceAccumulationTests(BaseFixture):
    """Video-mandatory (damaged-as-video here): no attachment -> ask video; the video
    reply creates ONE ticket (email is an identifier) with the video attached, and the
    video is never re-requested."""

    def setUp(self):
        super().setUp()
        from apps.brand_settings.models import BrandSettings
        from apps.taxonomy.models import Category, SubTopic
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        SubTopic.objects.create(category=cat, code="3.3", name="Damaged",
                                requires_evidence=True, requires_video=True,
                                mandatory_inputs=["order_id"])

    def _provider(self):
        import json as _json

        class FP:
            def generate(self, system, user):
                return _json.dumps({
                    "is_support_request": True, "category": "3. Delivery Issues",
                    "sub_topic": "3.3 Damaged", "confidence": 0.9,
                    "requires_evidence": True, "requires_agent": False,
                    "issue_summary": "damaged product", "sentiment": "neutral",
                    "extracted": {}})
        return FP()

    def _run(self, emails):
        from apps.classifier import service as classifier
        orig = classifier.build_provider
        classifier.build_provider = lambda s: self._provider()
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            classifier.build_provider = orig

    def test_video_reply_creates_one_ticket_no_reask(self):
        from apps.tickets.models import PendingConversation
        self._run([
            eml(subject="my product is damage", body="I received a damaged product",
                message_id="<a@x>"),
            # Damaged requires BOTH a photo AND a video -> supply both.
            eml(subject="Re: my product is damage", body="I send photo and video, order id 123456",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>",
                image=True, video=True)])
        self.assertEqual(Ticket.objects.count(), 1)             # video -> ONE ticket
        self.assertEqual(PendingConversation.objects.count(), 0)
        t = Ticket.objects.get()
        self.assertTrue(t.attachments.filter(content_type__startswith="video/").exists())
        self.assertEqual(t.extracted.get("order_id"), "123456")
        self.assertNotIn("care.deodap.in", t.tracking_url or "")  # no phone -> no 404 link


@override_settings(CARE_PANEL_STORE_TOKEN="test-token",  # Care Panel "configured"
                   PUBLIC_BASE_URL="https://support.deodap.in")  # portal configured
class PhoneNotRequiredTests(BaseFixture):
    """A missing phone must NOT block ticket creation -- the ticket is created on any one
    identifier (email here). But store-json is phone-keyed, so without a phone there is no
    Care Panel data.hash, and an internal hash is NEVER wrapped in a care.deodap.in URL
    (it would 404) -- so the ticket simply has NO tracking link."""

    def setUp(self):
        super().setUp()
        from apps.brand_settings.models import BrandSettings
        from apps.taxonomy.models import Category, SubTopic
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        SubTopic.objects.create(category=cat, code="3.3", name="Damaged",
                                requires_evidence=True, mandatory_inputs=["order_id"])

    def _provider(self):
        import json as _json

        class FP:
            def generate(self, system, user):
                return _json.dumps({
                    "is_support_request": True, "category": "3. Delivery Issues",
                    "sub_topic": "3.3 Damaged", "confidence": 0.9,
                    "requires_evidence": True, "requires_agent": False,
                    "issue_summary": "damaged", "sentiment": "neutral",
                    "extracted": {"order_id": "DD9999"}})
        return FP()

    def _run(self, emails):
        from apps.classifier import service as classifier
        orig = classifier.build_provider
        classifier.build_provider = lambda s: self._provider()
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            classifier.build_provider = orig

    def test_no_phone_still_creates_ticket_but_no_link(self):
        from apps.tickets.models import PendingConversation
        self._run([
            eml(subject="damaged", body="it broke order DD9999", message_id="<a@x>"),
            # photo + video + order id, NO phone -> ticket is STILL created (not blocked).
            eml(subject="Re: damaged", body="here is the photo and video", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True, video=True)])
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(PendingConversation.objects.count(), 0)
        t = Ticket.objects.get()
        self.assertEqual(t.extracted.get("phone"), None)            # no phone
        self.assertEqual(t.extracted.get("order_id"), "DD9999")
        self.assertEqual(t.tracking_url, "")                        # no Care Panel hash -> NO link
        self.assertNotIn("care.deodap.in", t.tracking_url)          # never an internal-hash 404
        self.assertTrue(t.extracted.get("tracking_hash"))           # hash kept for our /t portal

    def test_confirmation_without_phone_has_no_link(self):
        self._run([
            eml(subject="damaged", body="broke DD9999", message_id="<a@x>"),
            # Damaged requires BOTH a photo AND a video -> supply both.
            eml(subject="Re: damaged", body="photo and video", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True, video=True)])
        t = Ticket.objects.get()
        out = t.messages.filter(direction=Message.DIRECTION_OUTBOUND,
                                subject="Support Ticket Created Successfully").last()
        self.assertIsNotNone(out)                                   # confirmation still sent (M5N)
        self.assertNotIn("care.deodap.in/t?id=", out.body_text)    # no broken link in the email
