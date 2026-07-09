"""
FLOW 1 (existing-ticket reply) vs FLOW 2 (new-email duplicate detection).

A reply that threads into an existing ticket must NEVER run duplicate-ticket detection and must
NEVER receive an "Existing Ticket Found" (M6) mail. It is routed by a lightweight reply classifier
instead: STATUS_REQUEST / ADDITIONAL_INFORMATION / GENERAL_REPLY / NEW_ISSUE. A brand-new email
(no thread) keeps the normal flow, including duplicate detection.

    python manage.py test apps.ingestion.tests_reply_flow
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.ingestion import service
from apps.ingestion.tests_imap import FakeImap
from apps.ingestion.tests_smart import BaseFixture, eml
from apps.tickets.models import Attachment, Message, Ticket


# === Unit: the reply classifier (pure keyword/attachment routing) ==============================
class ReplyClassifierUnitTests(TestCase):
    def _c(self, body, subject="Re: your order", **extra):
        msg = {"subject": subject, "body_text": body, "from_email": "buyer@example.com"}
        msg.update(extra)
        return service._classify_reply(None, msg)

    def test_status_request(self):
        for b in ("Any update?", "status?", "Please update", "What is the latest status?",
                  "Can you check?", "any progress on this"):
            self.assertEqual(self._c(b), service.REPLY_STATUS_REQUEST, b)

    def test_general_reply_acknowledgements(self):
        for b in ("Thank you", "Thanks", "Done", "Okay", "Received", "thanks a lot",
                  "ok thanks", "Great, thank you", "Noted"):
            self.assertEqual(self._c(b), service.REPLY_GENERAL, b)

    def test_new_issue(self):
        for b in ("Also payment failed.", "Another issue.", "Refund not received.",
                  "I now have a different issue", "another order is missing"):
            self.assertEqual(self._c(b), service.REPLY_NEW_ISSUE, b)

    def test_additional_information_attachment(self):
        # An attachment (invoice / screenshot / video) -> additional information.
        pdf = {"attachment_blobs": [{"filename": "invoice.pdf", "mime_type": "application/pdf"}]}
        self.assertEqual(self._c("Please find attached invoice.", **pdf),
                         service.REPLY_ADDITIONAL_INFORMATION)
        img = {"attachment_blobs": [{"filename": "photo.png", "mime_type": "image/png"}]}
        self.assertEqual(self._c("Another screenshot.", **img),
                         service.REPLY_ADDITIONAL_INFORMATION)

    def test_substantive_text_defaults_to_additional_info(self):
        # Free text that is neither an ack, a status ask, nor a new issue -> attach + notify team
        # (never 'Existing Ticket Found').
        self.assertEqual(self._c("The replacement you sent is the wrong colour and size."),
                         service.REPLY_ADDITIONAL_INFORMATION)

    def test_quoted_history_is_ignored(self):
        # A plain "Thank you" that quotes the original complaint must still be GENERAL -- the
        # status / new-issue keywords in the quoted text must not leak in.
        body = ("Thank you\n\nOn Mon, 1 Jan 2026, buyer@example.com wrote:\n"
                "> any update? I have another issue, payment failed")
        self.assertEqual(self._c(body), service.REPLY_GENERAL)


# === Integration: FLOW 1 end-to-end (no AI needed) =============================================
class ReplyFlowTests(BaseFixture):
    def _open_ticket(self):
        service.fetch_imap(self.mailbox, client=FakeImap([
            eml(subject="Where is my order?", body="DD9999?", message_id="<a@x>")]))
        return Ticket.objects.get()

    def _reply(self, body, **kw):
        service.fetch_imap(self.mailbox, client=FakeImap([
            eml(subject="Re: Where is my order?", body=body, message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", **kw)], start_uid=2))

    def test_general_reply_saves_only_no_autoreply_no_existing_found(self):
        self._open_ticket()
        out_before = Message.objects.filter(direction=Message.DIRECTION_OUTBOUND).count()
        self._reply("Thank you")

        self.assertEqual(Ticket.objects.count(), 1)                 # no duplicate
        t = Ticket.objects.get()
        self.assertEqual(t.messages.filter(direction=Message.DIRECTION_INBOUND).count(), 2)  # saved
        # No new outbound (no auto-reply) and definitely NOT "Existing Ticket Found".
        self.assertEqual(Message.objects.filter(direction=Message.DIRECTION_OUTBOUND).count(),
                         out_before)
        self.assertFalse(Message.objects.filter(subject="Existing Ticket Found").exists())

    def test_status_request_sends_ticket_update(self):
        self._open_ticket()
        self._reply("any update?")

        self.assertEqual(Ticket.objects.count(), 1)
        t = Ticket.objects.get()
        upd = t.messages.filter(direction=Message.DIRECTION_OUTBOUND,
                                subject__startswith="Ticket Update").first()
        self.assertIsNotNone(upd)
        self.assertIn("Current Status:", upd.body_text)
        self.assertTrue(t.audit_log.filter(event="status_update_sent").exists())
        self.assertFalse(Message.objects.filter(subject="Existing Ticket Found").exists())

    def test_additional_information_attachment_attached_not_existing_found(self):
        self._open_ticket()
        self._reply("here is the photo", image=True)

        self.assertEqual(Ticket.objects.count(), 1)
        t = Ticket.objects.get()
        self.assertEqual(Attachment.objects.filter(ticket=t).count(), 1)
        self.assertFalse(Message.objects.filter(subject="Existing Ticket Found").exists())

    def test_flow2_new_email_duplicate_still_gets_existing_ticket_found(self):
        # FLOW 2 is UNCHANGED: a brand-new (UNthreaded) email duplicating an open ticket is merged
        # and DOES receive 'Existing Ticket Found' -- that path only fires for new emails, never
        # for a reply on an existing thread.
        self._open_ticket()
        service.fetch_imap(self.mailbox, client=FakeImap([
            eml(subject="Where is my order?", body="DD9999 still waiting",
                message_id="<b@x>")], start_uid=2))

        self.assertEqual(Ticket.objects.count(), 1)                 # merged, no duplicate
        t = Ticket.objects.get()
        self.assertTrue(t.audit_log.filter(event="ticket_updated").exists())
        # The merge notified the customer via the M6 'existing ticket' confirmation ('Existing
        # Ticket Found' with a Care Panel link, or the no-link 'Ticket Updated Successfully'
        # variant when -- as in tests -- Care Panel is not configured).
        self.assertTrue(Message.objects.filter(
            subject__in=["Existing Ticket Found", "Ticket Updated Successfully"]).exists())


# === NEW_ISSUE routing: a different issue forks its OWN ticket; the same issue just attaches ====
class ReplyNewIssueRoutingTests(BaseFixture):
    def _parent(self):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox, thread_id="<a@x>",
            customer_email="buyer@example.com", subject="Where is my order?",
            category="1. Shipment & Delivery Tracking", extracted={"order_id": "DD9999"})

    def _msg(self, body):
        return {"subject": "Re: Where is my order?", "body_text": body,
                "from_email": "buyer@example.com", "message_id": "<a2@x>",
                "gmail_message_id": "<a2@x>", "to_email": "care@deodap.com"}

    def test_different_category_opens_a_separate_ticket(self):
        parent = self._parent()
        fake = SimpleNamespace(category="8. Payment & Invoice", is_support_request=True,
                               sub_topic="8.1 Refund", category_ref=None, sub_topic_ref=None,
                               extracted={"order_id": "DD8888"})
        with patch.object(service, "_classify_dict", return_value=fake), \
             patch.object(service, "process_new_ticket") as pnt:
            result = service._handle_reply_new_issue(
                self.mailbox, parent, self._msg("payment failed for order DD8888"))

        self.assertEqual(Ticket.objects.count(), 2)                 # a separate ticket was forked
        self.assertNotEqual(result.id, parent.id)
        self.assertNotEqual(result.thread_id, parent.thread_id)     # its own thread
        self.assertTrue(pnt.called)                                 # ran the normal new-ticket flow
        self.assertFalse(Message.objects.filter(subject="Existing Ticket Found").exists())

    def test_same_category_attaches_without_a_new_ticket(self):
        parent = self._parent()
        fake = SimpleNamespace(category="1. Shipment & Delivery Tracking", is_support_request=True,
                               sub_topic="1.1", category_ref=None, sub_topic_ref=None,
                               extracted={"order_id": "DD9999"})
        with patch.object(service, "_classify_dict", return_value=fake):
            result = service._handle_reply_new_issue(
                self.mailbox, parent, self._msg("another issue, still waiting on my order"))

        self.assertEqual(Ticket.objects.count(), 1)                 # attached, not duplicated
        self.assertEqual(result.id, parent.id)
        self.assertTrue(parent.audit_log.filter(event="internal_note").exists())
        self.assertFalse(Message.objects.filter(subject="Existing Ticket Found").exists())
