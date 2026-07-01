"""
Tests for the Care Panel media upload (POST /t/add_comment). A fake session stands
in for the network so we assert the request shape without hitting the live API.

    python manage.py test apps.integrations.tests_care_panel_media
"""

from django.core.files.base import ContentFile
from django.test import TestCase

from apps.integrations import care_panel_media
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Attachment, Ticket


class FakeResp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


class FakeSession:
    def __init__(self, page_text='name="_token" value="CSRF123"', post_status=200):
        self.page_text = page_text
        self.post_status = post_status
        self.posted = None

    def get(self, url, timeout=None):
        self.get_url = url
        return FakeResp(200, self.page_text)

    def post(self, url, data=None, files=None, headers=None, timeout=None):
        self.posted = {"url": url, "data": data, "files": files}
        return FakeResp(self.post_status, "ok")


class MediaUploadTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self, hash_id="4zA2EVBwP1"):
        extracted = {"care_panel_ticket_id": hash_id} if hash_id else {}
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="damaged",
            tracking_url=f"https://care.deodap.in/t?id={hash_id}" if hash_id else "",
            extracted=extracted)
        for name, ct, body in [("photo.png", "image/png", b"IMG-bytes"),
                               ("clip.mp4", "video/mp4", b"VID-bytes")]:
            a = Attachment(ticket=t, filename=name, content_type=ct, size=len(body))
            a.file.save(name, ContentFile(body), save=True)   # distinct bytes
        return t

    def test_uploads_pending_media(self):
        t = self._ticket()
        sess = FakeSession()
        n = care_panel_media.upload_attachments(t, session=sess)
        self.assertEqual(n, 2)                                 # both files uploaded
        self.assertTrue(sess.posted["url"].endswith("/t/add_comment"))
        self.assertEqual(sess.posted["data"]["hashId"], "4zA2EVBwP1")
        self.assertEqual(sess.posted["data"]["_token"], "CSRF123")
        self.assertTrue(sess.posted["data"]["comment"])
        # ONE file per request now (batching made an oversized file sink the whole upload),
        # so the last recorded POST carries a single attachment.
        self.assertEqual(len(sess.posted["files"]), 1)
        self.assertEqual(sess.posted["files"][0][0], "attachments[]")
        # marked uploaded -> not re-sent
        self.assertEqual(care_panel_media.upload_attachments(t, session=FakeSession()), 0)
        self.assertTrue(t.audit_log.filter(event="care_panel_media_uploaded").exists())

    def test_skips_without_hash_id(self):
        t = self._ticket(hash_id="")
        self.assertEqual(care_panel_media.upload_attachments(t, session=FakeSession()), 0)

    def test_upload_failure_audited(self):
        t = self._ticket()
        n = care_panel_media.upload_attachments(t, session=FakeSession(post_status=500))
        self.assertEqual(n, 0)
        self.assertTrue(t.audit_log.filter(event="care_panel_media_failed").exists())


class MediaDedupTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def test_identical_image_uploaded_only_once(self):
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="defective",
            tracking_url="https://care.deodap.in/t?id=H1",
            extracted={"care_panel_ticket_id": "H1"})
        # Same image bytes attached on three different replies (the reported bug).
        same = b"WHATSAPP-IMAGE-SAME-BYTES"
        for i in range(3):
            a = Attachment(ticket=t, filename=f"img{i}.jpg", content_type="image/jpeg")
            a.file.save(f"img{i}.jpg", ContentFile(same), save=True)
        n = care_panel_media.upload_attachments(t, session=FakeSession())
        self.assertEqual(n, 1)                          # uploaded ONCE, not 3x
        # The other two are marked uploaded (deduped), none re-sent.
        self.assertEqual(t.attachments.filter(remote_url="").count(), 0)
        self.assertEqual(care_panel_media.upload_attachments(t, session=FakeSession()), 0)
