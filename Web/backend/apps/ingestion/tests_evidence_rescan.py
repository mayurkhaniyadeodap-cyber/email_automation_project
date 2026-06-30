"""
Regression: the system must NOT re-request evidence once the customer has attached a
photo/video. Covers the file-type scan + the decision-engine path that was re-asking
even though stored attachments existed.

    python manage.py test apps.ingestion.tests_evidence_rescan
"""

from django.test import TestCase

from apps.ingestion import evidence, service
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, Rule, SubTopic
from apps.tickets.models import Attachment, Message, Ticket


class ScanAttachmentsTests(TestCase):
    def test_photo_extensions(self):
        for fn in ["a.jpg", "a.jpeg", "a.PNG", "shot.webp"]:
            self.assertEqual(evidence.scan_attachments([(fn, "")]), (True, False), fn)

    def test_video_extensions(self):
        for fn in ["clip.mp4", "v.MOV", "x.avi", "y.mkv", "z.webm"]:
            self.assertEqual(evidence.scan_attachments([(fn, "")]), (False, True), fn)

    def test_mime_wins_over_missing_extension(self):
        self.assertEqual(evidence.scan_attachments([("file", "image/jpeg")]), (True, False))
        self.assertEqual(evidence.scan_attachments([("file", "video/mp4")]), (False, True))

    def test_non_evidence(self):
        self.assertEqual(evidence.scan_attachments([("invoice.pdf", "application/pdf")]),
                         (False, False))


class EngineNoReaskTests(TestCase):
    """The decision engine must NOT fire ACTION_AWAIT_EVIDENCE when the ticket already
    has stored photo/video attachments (the reported re-ask bug)."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        from apps.brand_settings.models import BrandSettings
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        self.cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        self.sub = SubTopic.objects.create(category=self.cat, code="3.3", name="Damaged",
                                           requires_evidence=True)
        # The seed's two-rule shape: ask if no evidence, else create.
        Rule.objects.create(sub_topic=self.sub, position=1,
                            condition="No unboxing video / photo evidence present",
                            then_response="Please share a clear photo of the damaged item.",
                            action=Rule.ACTION_AWAIT_EVIDENCE)
        Rule.objects.create(sub_topic=self.sub, position=2, condition="Evidence present",
                            then_response="Complaint registered; routed to agent.",
                            action=Rule.ACTION_CREATE_TICKET)

    def _ticket(self):
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="my product is damage",
            issue_summary="damaged product", category_ref=self.cat,
            sub_topic_ref=self.sub, ai_confidence=0.9,
            classification_status=Ticket.CLS_CLASSIFIED, extracted={})
        return t

    def test_no_attachment_asks_for_evidence(self):
        t = self._ticket()
        service._auto_decide(t)
        t.refresh_from_db()
        # No evidence -> the await-evidence rule fired.
        dec = t.audit_log.filter(event="decision").last()
        self.assertEqual(dec.detail["action"], Rule.ACTION_AWAIT_EVIDENCE)

    def test_stored_photo_stops_the_reask(self):
        t = self._ticket()
        # Customer attached a photo (stored, but extracted flags NOT set -- the bug case).
        Attachment.objects.create(ticket=t, filename="WhatsApp Image.jpeg",
                                  content_type="image/jpeg")
        service._auto_decide(t)
        t.refresh_from_db()
        # Engine must see the evidence and NOT re-ask.
        dec = t.audit_log.filter(event="decision").last()
        self.assertNotEqual(dec.detail["action"], Rule.ACTION_AWAIT_EVIDENCE)
        self.assertEqual(dec.detail["action"], Rule.ACTION_CREATE_TICKET)
        self.assertTrue(t.extracted.get("has_photo"))      # flag synced from the file

    def test_stored_video_stops_the_reask(self):
        t = self._ticket()
        Attachment.objects.create(ticket=t, filename="clip.mp4", content_type="video/mp4")
        service._auto_decide(t)
        t.refresh_from_db()
        dec = t.audit_log.filter(event="decision").last()
        self.assertEqual(dec.detail["action"], Rule.ACTION_CREATE_TICKET)
        self.assertTrue(t.extracted.get("has_unboxing_video"))

    def test_extension_only_no_mime_still_detected(self):
        t = self._ticket()
        # Gmail-path attachment with a filename but blank content_type.
        Attachment.objects.create(ticket=t, filename="myvideo.mov", content_type="")
        service._auto_decide(t)
        t.refresh_from_db()
        self.assertEqual(t.audit_log.filter(event="decision").last().detail["action"],
                         Rule.ACTION_CREATE_TICKET)
