"""
Offline tests for IMAP ingestion: RFC822 parsing + the fetch->ingest->thread flow
with a fake IMAP client (no network).

    python manage.py test apps.ingestion.tests_imap
"""

from email.message import EmailMessage

from django.test import TestCase, override_settings

from apps.ingestion import service, smtp_client
from apps.ingestion.imap_client import parse_rfc822
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Message, Ticket


def build_eml(*, subject, from_addr, body, message_id, to="care@deodap.com",
              in_reply_to=None, references=None, html=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


class FakeImap:
    """Assigns synthetic UIDs start_uid.. and only returns UID > last_uid."""

    def __init__(self, raw_messages, start_uid=1, validity=1):
        self._raw = raw_messages
        self.start_uid = start_uid
        self.validity = validity

    def fetch_new(self, last_uid=0, uidvalidity=None, limit=None):
        items = []
        uid = self.start_uid
        for r in self._raw:
            if uid > (last_uid or 0):
                items.append((uid, parse_rfc822(r)))
            uid += 1
        return self.validity, items

    def fetch_recent(self, limit=None, unseen_only=False):
        return [parse_rfc822(r) for r in self._raw]


class ParseTests(TestCase):
    def test_parses_plain_email(self):
        raw = build_eml(
            subject="Where is my order DD123?",
            from_addr="Buyer <buyer@example.com>",
            body="Hi, where is order DD123?",
            message_id="<m1@example.com>",
        )
        n = parse_rfc822(raw)
        self.assertEqual(n["from_email"], "buyer@example.com")
        self.assertEqual(n["subject"], "Where is my order DD123?")
        self.assertIn("DD123", n["body_text"])
        self.assertEqual(n["message_id"], "<m1@example.com>")

    def test_parses_references_for_threading(self):
        raw = build_eml(
            subject="Re: order", from_addr="b@x.com", body="any update?",
            message_id="<m2@x.com>", in_reply_to="<m1@x.com>",
            references="<m1@x.com>",
        )
        n = parse_rfc822(raw)
        self.assertEqual(n["in_reply_to"], "<m1@x.com>")
        self.assertEqual(n["references"], ["<m1@x.com>"])


class FetchImapTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(
            brand=self.brand, email_address="care@deodap.com"
        )

    def test_fetch_creates_tickets_and_dedups(self):
        raw = [
            build_eml(subject="Order DD1?", from_addr="a@x.com", body="where",
                      message_id="<a@x.com>"),
            build_eml(subject="Refund?", from_addr="b@x.com", body="refund please",
                      message_id="<b@x.com>"),
        ]
        inbound = Message.objects.filter(direction=Message.DIRECTION_INBOUND)
        client = FakeImap(raw)
        r1 = service.fetch_imap(self.mailbox, client=client)
        self.assertEqual(len(r1), 2)
        self.assertEqual(Ticket.objects.count(), 2)
        self.assertEqual(inbound.count(), 2)
        # Re-fetch the same mail -> deduped on Message-ID, no new tickets/mail.
        service.fetch_imap(self.mailbox, client=FakeImap(raw))
        self.assertEqual(Ticket.objects.count(), 2)
        self.assertEqual(inbound.count(), 2)

    def test_uid_watermark_skips_old_mail(self):
        raw = [build_eml(subject="A", from_addr="a@x.com", body="hi", message_id="<a@x>"),
               build_eml(subject="B", from_addr="b@x.com", body="hi", message_id="<b@x>")]
        service.fetch_imap(self.mailbox, client=FakeImap(raw))  # uids 1,2
        self.mailbox.refresh_from_db()
        self.assertEqual(self.mailbox.imap_last_uid, 2)  # watermark advanced
        # A third email arrives with a higher UID -> only it is fetched.
        third = build_eml(subject="C", from_addr="c@x.com", body="hi", message_id="<c@x>")
        results = service.fetch_imap(self.mailbox, client=FakeImap([third], start_uid=3))
        new = [r for r in results if r[2]]  # created == True
        self.assertEqual(len(new), 1)
        self.mailbox.refresh_from_db()
        self.assertEqual(self.mailbox.imap_last_uid, 3)
        self.assertEqual(Ticket.objects.count(), 3)

    def test_refetch_returns_no_new(self):
        raw = [build_eml(subject="A", from_addr="a@x.com", body="hi", message_id="<a@x>")]
        service.fetch_imap(self.mailbox, client=FakeImap(raw))
        # Same mail, same UID -> nothing new on re-fetch.
        results = service.fetch_imap(self.mailbox, client=FakeImap(raw))
        self.assertEqual([r for r in results if r[2]], [])

    def test_reply_threads_into_same_ticket(self):
        first = build_eml(subject="Order DD1?", from_addr="a@x.com", body="where",
                          message_id="<a@x.com>")
        service.fetch_imap(self.mailbox, client=FakeImap([first]))
        reply = build_eml(subject="Re: Order DD1?", from_addr="a@x.com",
                          body="any update?", message_id="<a2@x.com>",
                          in_reply_to="<a@x.com>", references="<a@x.com>")
        # The reply arrives with a higher UID than the first message.
        service.fetch_imap(self.mailbox, client=FakeImap([reply], start_uid=2))
        self.assertEqual(Ticket.objects.count(), 1)
        inbound = Ticket.objects.first().messages.filter(
            direction=Message.DIRECTION_INBOUND
        )
        self.assertEqual(inbound.count(), 2)


@override_settings(EMAIL_PROVIDER="imap")
class SendReplySmtpTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.ticket = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="buyer@example.com", subject="Order DD1?", thread_id="t1",
        )
        Message.objects.create(
            ticket=self.ticket, direction=Message.DIRECTION_INBOUND,
            from_email="buyer@example.com", subject="Order DD1?", body_text="where?",
            headers={"Message-ID": "<m1@x>"},
        )
        self.outbound = Message.objects.create(
            ticket=self.ticket, direction=Message.DIRECTION_OUTBOUND,
            to_email="buyer@example.com", subject="Re: Order DD1?",
            body_text="Your order is on the way!",
        )

    def test_send_reply_routes_to_smtp(self):
        calls = []
        original = smtp_client.send_email
        smtp_client.send_email = lambda **kw: (calls.append(kw) or "<sent-1@x>")
        try:
            sent_id = service.send_reply(self.outbound)
        finally:
            smtp_client.send_email = original

        self.assertEqual(sent_id, "<sent-1@x>")
        self.assertEqual(calls[0]["to"], "buyer@example.com")
        self.assertEqual(calls[0]["in_reply_to"], "<m1@x>")  # threads the reply
        self.outbound.refresh_from_db()
        self.assertEqual(self.outbound.gmail_message_id, "<sent-1@x>")
        self.assertIsNotNone(self.outbound.sent_at)


class AttachmentIngestTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _eml_with_image(self):
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = "Damaged product"
        msg["From"] = "buyer@example.com"
        msg["To"] = "care@deodap.com"
        msg["Message-ID"] = "<att1@x>"
        msg.set_content("Photo attached.")
        msg.add_attachment(b"\x89PNG\r\n\x1a\nFAKEPNGDATA", maintype="image",
                           subtype="png", filename="damage.png")
        return msg.as_bytes()

    def test_attachment_stored_on_ingest(self):
        from apps.tickets.models import Attachment
        service.fetch_imap(self.mailbox, client=FakeImap([self._eml_with_image()]))
        att = Attachment.objects.first()
        self.assertIsNotNone(att)
        self.assertEqual(att.filename, "damage.png")
        self.assertEqual(att.content_type, "image/png")
        self.assertEqual(att.kind, "image")
        self.assertTrue(att.size > 0)
        self.assertTrue(att.file.name)  # a file was written
        att.file.delete(save=False)  # cleanup
