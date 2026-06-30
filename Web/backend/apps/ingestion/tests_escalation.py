"""
High-priority escalation queue: a legal / consumer-court / grievance / negative-review email
STOPS all automation (no classification, verification, tracking, evidence, auto-reply, ticket)
and lands in the MANUAL_REVIEW queue. The customer receives NO automatic email.

    python manage.py test apps.ingestion.tests_escalation
"""
from django.test import TestCase

from apps.ingestion import service
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Escalation, PendingConversation, Ticket


class EscalationQueueTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.sent = []
        self._oe = service._send_customer_email
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body}) or "<sent>")
        # Classification must NEVER run for an escalation -> make it explode if it does.
        self._oc = service._classify_dict
        service._classify_dict = lambda b, m: (_ for _ in ()).throw(
            AssertionError("classification ran for an escalation email"))

    def tearDown(self):
        service._send_customer_email = self._oe
        service._classify_dict = self._oc

    def _run(self, subject, body, mid="<e1@x>"):
        return service.handle_incoming_email(
            self.mailbox, {"subject": subject, "body_text": body, "from_email": "buyer@x.com",
                           "message_id": mid, "gmail_message_id": mid})

    def _assert_escalated(self, keyword):
        esc = Escalation.objects.get()
        self.assertEqual(esc.status, Escalation.STATUS_MANUAL_REVIEW)
        self.assertEqual(esc.priority, "high")
        self.assertEqual(esc.queue, "escalation")
        self.assertEqual(esc.matched_keyword, keyword)
        self.assertEqual(esc.sender, "buyer@x.com")
        self.assertEqual(Ticket.objects.count(), 0)               # NO ticket
        self.assertEqual(PendingConversation.objects.count(), 0)  # NO pending / evidence flow
        self.assertEqual(self.sent, [])                           # NO automatic email
        return esc

    # --- the 5 required tests ----------------------------------------------------------------
    def test_nch_moves_to_manual_review_queue(self):
        t, m, created = self._run("", "I have filed an NCH complaint against you")
        self.assertIsNone(t)
        esc = self._assert_escalated("NCH")
        self.assertEqual(esc.status, Escalation.STATUS_MANUAL_REVIEW)

    def test_consumer_court_skips_ticket_creation(self):
        self._run("Refund", "I will take you to consumer court for this damaged item")
        self._assert_escalated("CONSUMER COURT")
        self.assertEqual(Ticket.objects.count(), 0)

    def test_lawyer_skips_auto_reply(self):
        self._run("", "My lawyer will send you a legal notice")
        self._assert_escalated("LAWYER")
        self.assertEqual(self.sent, [])

    def test_negative_review_skips_verification(self):
        # Body carries an order id + mobile -> verification would normally run; it must NOT.
        self._run("", "I will post a negative review. order id 262324646 mobile 9876543210")
        self._assert_escalated("NEGATIVE REVIEW")

    def test_police_complaint_requires_manual_review(self):
        self._run("", "filing a police complaint and cyber crime report")
        esc = self._assert_escalated("POLICE COMPLAINT")
        self.assertEqual(esc.status, Escalation.STATUS_MANUAL_REVIEW)

    def test_customer_reply_matches_open_escalation_by_sender(self):
        # An escalation is open and the agent has replied (awaiting customer).
        self._run("", "I will file a CYBER CRIME complaint", mid="<e1@x>")
        esc = Escalation.objects.get()
        esc.status = Escalation.STATUS_AWAITING_REPLY
        esc.save(update_fields=["status"])
        # Customer replies with EVIDENCE. Its refs DON'T match the stored thread_ids (agent had
        # replied from a different alias / Gmail rewrote the id), but it's a reply from the SAME
        # sender -> must append to the open escalation, NOT spawn a new one or run automation.
        t, m, created = service.handle_incoming_email(self.mailbox, {
            "subject": "Re: complaint", "body_text": "here is my photo and video evidence",
            "from_email": "buyer@x.com", "message_id": "<e2@x>", "gmail_message_id": "<e2@x>",
            "in_reply_to": "<does-not-match@gmail.com>", "references": "<does-not-match@gmail.com>"})
        self.assertIsNone(t)
        self.assertEqual(Escalation.objects.count(), 1)          # NO new escalation
        esc.refresh_from_db()
        self.assertEqual(esc.conversation[-1]["body"], "here is my photo and video evidence")
        self.assertEqual(esc.status, Escalation.STATUS_MANUAL_REVIEW)  # back to review

    # --- behaviour guards --------------------------------------------------------------------
    def test_logs_emitted(self):
        with self.assertLogs("apps.ingestion.service", level="INFO") as cm:
            self._run("", "grievance against deodap")
        blob = "\n".join(cm.output)
        self.assertIn("ESCALATION-DETECTED", blob)
        self.assertIn("ESCALATION-KEYWORD=GRIEVANCE", blob)
        self.assertIn("SENDER=buyer@x.com", blob)

    def test_duplicate_email_not_double_recorded(self):
        self._run("", "consumer forum complaint", mid="<dup@x>")
        self._run("", "consumer forum complaint", mid="<dup@x>")
        self.assertEqual(Escalation.objects.count(), 1)

    def test_normal_email_not_escalated(self):
        # A plain tracking query must NOT escalate (no keyword) -> classification runs (and our
        # patched classifier raises, proving the escalation gate let it through).
        with self.assertRaises(AssertionError):
            self._run("", "where is my order mobile 9876543210")
        self.assertEqual(Escalation.objects.count(), 0)

    def test_email_address_does_not_false_trigger_keyword(self):
        # 'owner@shop.com' / 'press@x.com' are email addresses, NOT the OWNER/PRESS keyword ->
        # must NOT escalate (addresses are stripped before keyword search).
        with self.assertRaises(AssertionError):       # classification runs -> not escalated
            self._run("refund", "refund order 000000 email owner@shop.com")
        self.assertEqual(Escalation.objects.count(), 0)

    def test_attachment_text_triggers_escalation(self):
        # An escalation keyword hidden in an attachment's OCR/PDF text must still escalate.
        service.handle_incoming_email(self.mailbox, {
            "subject": "document", "body_text": "please see attached", "from_email": "buyer@x.com",
            "message_id": "<att@x>", "gmail_message_id": "<att@x>",
            "attachment_blobs": [{"text": "This is a LEGAL NOTICE from my advocate."}]})
        esc = Escalation.objects.get()
        self.assertEqual(esc.matched_keyword, "LEGAL NOTICE")
        self.assertEqual(Ticket.objects.count(), 0)


class EscalationActionTests(TestCase):
    """Agent actions on an escalation: reply (threaded) / create-ticket / resolve / ignore.
    Driven through the REAL API (auth + URL routing) via APIClient."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.agent = get_user_model().objects.create_superuser(
            username="alice", email="agent@x.com", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.agent)
        self.sent = []
        self._oe = service._send_customer_email
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body, **k}) or "<reply-1@x>")

    def tearDown(self):
        service._send_customer_email = self._oe

    def _post(self, esc, action, **data):
        return self.client.post(f"/api/escalations/{esc.id}/{action}/", data, format="json")

    def _esc(self):
        return Escalation.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            sender="customer@x.com", subject="legal notice", body="my lawyer will sue you",
            matched_keyword="LAWYER", message_id="<orig@x>", thread_ids=["<orig@x>"],
            status=Escalation.STATUS_MANUAL_REVIEW)

    def test_reply_sends_email_to_customer(self):
        esc = self._esc()
        service.send_escalation_reply(esc, "We are reviewing your concern.", agent="alice")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["to"], "customer@x.com")
        self.assertIn("reviewing your concern", self.sent[0]["body"])
        esc.refresh_from_db()
        self.assertEqual(esc.status, Escalation.STATUS_AWAITING_REPLY)

    def test_reply_preserves_email_thread(self):
        esc = self._esc()
        service.send_escalation_reply(esc, "Hello", agent="alice")
        # In-Reply-To = the last thread id; References include the original.
        self.assertEqual(self.sent[0]["in_reply_to"], "<orig@x>")
        self.assertIn("<orig@x>", self.sent[0]["references"])
        esc.refresh_from_db()
        # The sent reply's Message-ID is recorded so the customer's reply matches this thread.
        self.assertIn("<reply-1@x>", esc.thread_ids)
        self.assertEqual(esc.conversation[-1]["direction"], "outbound")

    def test_customer_reply_continues_same_escalation(self):
        esc = self._esc()
        service.send_escalation_reply(esc, "Hello", agent="alice")
        # Customer replies, referencing our sent message -> appended to the SAME escalation.
        service.handle_incoming_email(self.mailbox, {
            "subject": "Re: legal notice", "body_text": "ok thanks", "from_email": "customer@x.com",
            "message_id": "<cust2@x>", "gmail_message_id": "<cust2@x>",
            "in_reply_to": "<reply-1@x>", "references": "<orig@x> <reply-1@x>"})
        self.assertEqual(Escalation.objects.count(), 1)          # no new escalation/ticket
        self.assertEqual(Ticket.objects.count(), 0)
        esc.refresh_from_db()
        self.assertEqual(esc.conversation[-1]["body"], "ok thanks")
        self.assertEqual(esc.status, Escalation.STATUS_MANUAL_REVIEW)

    def _capture_confirmations(self):
        confirmations, self._media = [], []
        self._oc, self._om = service.send_confirmation, service._upload_care_panel_media
        service.send_confirmation = lambda t, kind: confirmations.append((t, kind))
        service._upload_care_panel_media = lambda t: self._media.append(t)
        self.addCleanup(lambda: setattr(service, "send_confirmation", self._oc))
        self.addCleanup(lambda: setattr(service, "_upload_care_panel_media", self._om))
        return confirmations

    def test_create_ticket_from_escalation(self):
        # Default notify=True -> the customer IS emailed + media pushed to the Care Panel.
        confirmations = self._capture_confirmations()
        esc = self._esc()
        resp = self._post(esc, "create-ticket")
        self.assertEqual(resp.status_code, 200)
        esc.refresh_from_db()
        self.assertEqual(esc.status, Escalation.STATUS_TICKET_CREATED)
        self.assertIsNotNone(esc.ticket)
        self.assertEqual(esc.ticket.priority, Ticket.PRIORITY_HIGH)
        self.assertEqual(esc.ticket.status, Ticket.STATUS_ESCALATED)
        self.assertEqual([k for _, k in confirmations], ["created"])   # confirmation sent
        self.assertEqual(len(self._media), 1)                          # media pushed to Care Panel

    def test_create_ticket_notify_false_skips_email(self):
        # notify=False -> internal only, NO customer email (pure-legal case).
        confirmations = self._capture_confirmations()
        esc = self._esc()
        resp = self._post(esc, "create-ticket", notify=False)
        self.assertEqual(resp.status_code, 200)
        esc.refresh_from_db()
        self.assertEqual(esc.status, Escalation.STATUS_TICKET_CREATED)
        self.assertEqual(confirmations, [])                            # NO email

    def test_create_ticket_extracts_phone_for_link(self):
        # Escalation body carries the customer's phone -> it must land on the ticket so the
        # Care Panel store can build a tracking link (phone-keyed).
        self._capture_confirmations()
        esc = Escalation.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            sender="customer@x.com", subject="missing item",
            body="hii my number is 9994132234 please send the missing item",
            matched_keyword="MISSING", message_id="<m@x>", thread_ids=["<m@x>"],
            status=Escalation.STATUS_MANUAL_REVIEW)
        self._post(esc, "create-ticket", notify=False)
        esc.refresh_from_db()
        self.assertEqual((esc.ticket.extracted or {}).get("phone"), "9994132234")

    def test_create_ticket_carries_escalation_media(self):
        from apps.tickets.models import Attachment
        self._capture_confirmations()        # stub out the real Care Panel / email
        esc = self._esc()
        Attachment.objects.create(escalation=esc, filename="photo.png",
                                  content_type="image/png", size=10)
        self._post(esc, "create-ticket", notify=False)
        esc.refresh_from_db()
        # The escalation's photo is now attached to the ticket + flagged as media.
        self.assertEqual(esc.ticket.attachments.count(), 1)
        self.assertTrue((esc.ticket.extracted or {}).get("has_photo"))

    def test_reply_via_api(self):
        esc = self._esc()
        resp = self._post(esc, "reply", body="We are reviewing your concern.")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.sent[0]["to"], "customer@x.com")
        esc.refresh_from_db()
        self.assertEqual(esc.status, Escalation.STATUS_AWAITING_REPLY)

    def test_reply_send_failure_is_surfaced(self):
        # When the email send fails (SMTP returns None), the reply must NOT be reported as sent:
        # 502 + send_failed, status stays MANUAL_REVIEW, and the entry is flagged failed.
        service._send_customer_email = lambda *a, **k: None      # simulate SMTP failure
        esc = self._esc()
        resp = self._post(esc, "reply", body="hello")
        self.assertEqual(resp.status_code, 502)
        self.assertTrue(resp.data["send_failed"])
        esc.refresh_from_db()
        self.assertEqual(esc.status, Escalation.STATUS_MANUAL_REVIEW)   # NOT awaiting reply
        self.assertTrue(esc.conversation[-1]["failed"])

    def test_reply_with_attachments_and_subject(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from apps.tickets.models import Attachment
        esc = self._esc()
        f1 = SimpleUploadedFile("doc1.pdf", b"%PDF-1.4 data", content_type="application/pdf")
        f2 = SimpleUploadedFile("img.png", b"\x89PNGdata", content_type="image/png")
        resp = self.client.post(f"/api/escalations/{esc.id}/reply/",
                                {"body": "see attached", "subject": "Re: legal matter",
                                 "attachments": [f1, f2]}, format="multipart")
        self.assertEqual(resp.status_code, 200)
        # Email carried both files (filename, bytes, content_type tuples).
        self.assertEqual(len(self.sent[0]["attachments"]), 2)
        # Stored on the escalation + recorded in the conversation for history.
        self.assertEqual(Attachment.objects.filter(escalation=esc).count(), 2)
        esc.refresh_from_db()
        out = esc.conversation[-1]
        self.assertEqual(out["subject"], "Re: legal matter")
        self.assertEqual([a["filename"] for a in out["attachments"]], ["doc1.pdf", "img.png"])
        self.assertTrue(out["attachments"][0]["url"].startswith("/api/attachments/"))

    def test_resolve_escalation(self):
        esc = self._esc()
        resp = self._post(esc, "resolve")
        self.assertEqual(resp.status_code, 200)
        esc.refresh_from_db()
        self.assertEqual(esc.status, Escalation.STATUS_RESOLVED)
        self.assertEqual(esc.resolved_by, "agent@x.com")
        self.assertEqual(Ticket.objects.count(), 0)             # no ticket

    def test_ignore_escalation(self):
        esc = self._esc()
        resp = self._post(esc, "ignore")
        self.assertEqual(resp.status_code, 200)
        esc.refresh_from_db()
        self.assertEqual(esc.status, Escalation.STATUS_IGNORED)
        self.assertEqual(Ticket.objects.count(), 0)             # no ticket


class HelpdeskUITests(TestCase):
    """The Gmail/Zendesk-style split-view behaviours: open/detail, internal notes, assignment,
    timeline ordering, attachment preview, read-only resolve, history-preserving ignore."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.agent = get_user_model().objects.create_superuser("rahul", "rahul@x.com", "x")
        self.client = APIClient()
        self.client.force_authenticate(self.agent)
        self.sent = []
        self._oe = service._send_customer_email
        service._send_customer_email = lambda to, s, b, **k: (self.sent.append((to, b)) or "<r@x>")

    def tearDown(self):
        service._send_customer_email = self._oe

    def _make(self, **kw):
        defaults = dict(organization=self.org, brand=self.brand, mailbox=self.mailbox,
                        sender="customer@x.com", subject="legal notice", body="my lawyer",
                        matched_keyword="LAWYER", message_id="<o@x>", thread_ids=["<o@x>"],
                        conversation=[{"direction": "inbound", "body": "my lawyer",
                                       "message_id": "<o@x>", "from": "customer@x.com"}])
        defaults.update(kw)
        return Escalation.objects.create(**defaults)

    def _post(self, esc, action, **data):
        return self.client.post(f"/api/escalations/{esc.id}/{action}/", data, format="json")

    def test_open_escalation_detail(self):
        esc = self._make(is_read=False)
        resp = self.client.get(f"/api/escalations/{esc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("conversation", resp.data)
        self.assertEqual(resp.data["message_id"], "<o@x>")
        esc.refresh_from_db()
        self.assertTrue(esc.is_read)                 # opening marks it read

    def test_reply_keeps_thread(self):
        esc = self._make()
        self._post(esc, "reply", body="reviewing")
        esc.refresh_from_db()
        self.assertIn("<r@x>", esc.thread_ids)       # sent reply id joins the thread
        self.assertEqual(esc.status, Escalation.STATUS_AWAITING_REPLY)

    def test_customer_reply_same_escalation(self):
        esc = self._make()
        service.send_escalation_reply(esc, "hi", agent="rahul")
        service.handle_incoming_email(self.mailbox, {
            "subject": "Re: legal notice", "body_text": "still unhappy", "from_email": "customer@x.com",
            "message_id": "<c2@x>", "gmail_message_id": "<c2@x>",
            "in_reply_to": "<r@x>", "references": "<o@x> <r@x>"})
        self.assertEqual(Escalation.objects.count(), 1)    # same escalation, no new one
        self.assertEqual(Ticket.objects.count(), 0)
        esc.refresh_from_db()
        self.assertEqual(esc.conversation[-1]["body"], "still unhappy")

    def test_internal_note_not_emailed(self):
        esc = self._make()
        self._post(esc, "note", note="customer is aggressive, handle carefully")
        self.assertEqual(self.sent, [])              # NEVER emailed
        esc.refresh_from_db()
        note = esc.conversation[-1]
        self.assertEqual(note["direction"], "note")
        self.assertIn("aggressive", note["body"])

    def test_create_ticket_moves_queue(self):
        esc = self._make()
        self._oc = service.send_confirmation
        service.send_confirmation = lambda t, k: None
        try:
            self._post(esc, "create-ticket")
        finally:
            service.send_confirmation = self._oc
        esc.refresh_from_db()
        self.assertEqual(esc.status, Escalation.STATUS_TICKET_CREATED)   # out of open queue
        self.assertIsNotNone(esc.ticket)
        # No longer counted as an open escalation.
        self.assertEqual(Escalation.objects.exclude(
            status__in=Escalation.TERMINAL_STATUSES + [Escalation.STATUS_TICKET_CREATED]).count(), 0)

    def test_resolve_readonly(self):
        esc = self._make()
        self._post(esc, "resolve")
        esc.refresh_from_db()
        self.assertEqual(esc.status, Escalation.STATUS_RESOLVED)
        self.assertIn(esc.status, Escalation.TERMINAL_STATUSES)   # terminal -> read-only in UI

    def test_ignore_keeps_history(self):
        esc = self._make()
        service.add_escalation_note(esc, "note before ignore", agent="rahul")
        self._post(esc, "ignore")
        esc.refresh_from_db()
        self.assertEqual(esc.status, Escalation.STATUS_IGNORED)
        self.assertTrue(any(m.get("direction") == "note" for m in esc.conversation))  # history kept

    def test_assignment(self):
        esc = self._make()
        self._post(esc, "assign", assigned_to="rahul@x.com")
        esc.refresh_from_db()
        self.assertEqual(esc.assigned_to, "rahul@x.com")
        self.assertIsNotNone(esc.assigned_at)
        self.assertTrue(any(e["event"] == "assigned" for e in esc.timeline))

    def test_timeline_order(self):
        esc = self._make()
        esc.add_event("received", actor="customer"); esc.save()
        self._post(esc, "assign", assigned_to="rahul")
        self._post(esc, "note", note="checking")
        self._oc = service.send_confirmation
        service.send_confirmation = lambda t, k: None
        try:
            self._post(esc, "create-ticket")
        finally:
            service.send_confirmation = self._oc
        esc.refresh_from_db()
        events = [e["event"] for e in esc.timeline]
        # received -> assigned -> internal_note -> ticket_created, in order.
        self.assertEqual([e for e in events if e in
                          ("assigned", "internal_note", "ticket_created")],
                         ["assigned", "internal_note", "ticket_created"])

    def test_customer_reply_attachment_captured(self):
        # A customer reply that carries an attachment -> stored + shown in the conversation.
        from apps.tickets.models import Attachment
        esc = self._make()
        service.send_escalation_reply(esc, "hi", agent="rahul")
        service.handle_incoming_email(self.mailbox, {
            "subject": "Re: legal notice", "body_text": "see my proof", "from_email": "customer@x.com",
            "message_id": "<c3@x>", "gmail_message_id": "<c3@x>",
            "in_reply_to": "<r@x>", "references": "<o@x> <r@x>",
            "attachment_blobs": [{"filename": "proof.png", "mime_type": "image/png",
                                  "content": b"\x89PNGproof"}]})
        esc.refresh_from_db()
        last = esc.conversation[-1]
        self.assertEqual(last["body"], "see my proof")
        self.assertEqual(last["attachments"][0]["filename"], "proof.png")
        self.assertTrue(last["attachments"][0]["url"].startswith("/api/attachments/"))
        self.assertTrue(Attachment.objects.filter(escalation=esc, filename="proof.png").exists())

    def test_attachment_preview(self):
        service.handle_incoming_email(self.mailbox, {
            "subject": "doc", "body_text": "see attached legal notice", "from_email": "buyer@x.com",
            "message_id": "<att2@x>", "gmail_message_id": "<att2@x>",
            "attachment_blobs": [{"filename": "notice.pdf", "mime_type": "application/pdf",
                                  "url": "https://x/notice.pdf"}]})
        esc = Escalation.objects.get(message_id="<att2@x>")
        self.assertEqual(esc.attachments[0]["filename"], "notice.pdf")
        self.assertEqual(esc.attachments[0]["url"], "https://x/notice.pdf")
