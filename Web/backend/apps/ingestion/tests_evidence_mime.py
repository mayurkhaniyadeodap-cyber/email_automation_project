"""
Regression: an evidence reply whose attachment has a GENERIC MIME type
(application/octet-stream) but a real image/video filename (.jpg / .mp4 / .webp / .mov)
must still be detected as evidence -- stored on the pending conversation and used to
satisfy the evidence gate so the ticket is created.

The bug: _store_pending_attachments filtered by MIME only, so an octet-stream photo.jpg
was never stored; the extension-based re-scan then found nothing and the ticket was
never created (the customer kept getting evidence requests).

    python manage.py test apps.ingestion.tests_evidence_mime
"""

from django.test import TestCase

from apps.ingestion import service
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import PendingConversation


class PendingAttachmentMimeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _pending(self):
        return PendingConversation.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="buyer@example.com", subject="damaged item",
            status="awaiting_evidence", has_evidence=False, has_photo=False, has_video=False,
        )

    def _blob(self, content, filename, mime):
        return {"content": content, "filename": filename, "mime_type": mime}

    def test_octet_stream_jpg_is_stored_and_marks_photo(self):
        pending = self._pending()
        message = {"attachment_blobs": [
            self._blob(b"\xff\xd8\xff fake jpeg bytes", "photo.jpg", "application/octet-stream")]}
        service._accumulate_pending(pending, message)
        pending.refresh_from_db()
        self.assertEqual(pending.attachments.count(), 1)        # stored despite generic MIME
        self.assertTrue(pending.has_evidence)
        self.assertTrue(pending.has_photo)
        self.assertFalse(pending.has_video)

    def test_octet_stream_mp4_is_stored_and_marks_video(self):
        pending = self._pending()
        message = {"attachment_blobs": [
            self._blob(b"\x00\x00\x00 fake mp4 bytes", "unboxing.MP4", "application/octet-stream")]}
        service._accumulate_pending(pending, message)
        pending.refresh_from_db()
        self.assertEqual(pending.attachments.count(), 1)
        self.assertTrue(pending.has_evidence)
        self.assertTrue(pending.has_video)

    def test_multiple_attachments_mixed_mime(self):
        pending = self._pending()
        message = {"attachment_blobs": [
            self._blob(b"img1", "a.webp", "application/octet-stream"),
            self._blob(b"img2", "b.jpeg", "image/jpeg"),
            self._blob(b"vid1", "c.mov", ""),                  # extension-only, blank MIME
        ]}
        service._accumulate_pending(pending, message)
        pending.refresh_from_db()
        self.assertEqual(pending.attachments.count(), 3)        # all evidence files stored
        self.assertTrue(pending.has_photo)
        self.assertTrue(pending.has_video)

    def test_non_evidence_file_not_stored(self):
        pending = self._pending()
        message = {"attachment_blobs": [
            self._blob(b"%PDF-1.4 invoice", "invoice.pdf", "application/pdf")]}
        service._accumulate_pending(pending, message)
        pending.refresh_from_db()
        self.assertEqual(pending.attachments.count(), 0)        # a PDF is not photo/video evidence
        self.assertFalse(pending.has_evidence)

    def test_message_helpers_detect_by_extension(self):
        msg = {"attachment_blobs": [
            {"filename": "x.jpg", "mime_type": "application/octet-stream", "content": b"x"}]}
        self.assertTrue(service._message_has_evidence(msg))
        self.assertFalse(service._message_has_video(msg))
        vid = {"attachment_blobs": [
            {"filename": "x.mp4", "mime_type": "application/octet-stream", "content": b"x"}]}
        self.assertTrue(service._message_has_video(vid))
