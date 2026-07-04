"""
Tests for order-ID duplicate detection (Ticket Logic Rule 1).

    python manage.py test apps.ingestion.tests_dedup
"""

from django.test import TestCase, override_settings

from apps.classifier.service import ClassificationResult
from apps.ingestion import service
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Message, PendingConversation, ProcessedEmail, Ticket


class OrderDedupTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self, *, thread, order_id, category="1. Shipment", email="b@x.com",
                status=Ticket.STATUS_AWAITING_AGENT):
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            thread_id=thread, customer_email=email, subject="order " + order_id,
            category=category, status=status, extracted={"order_id": order_id},
        )
        Message.objects.create(ticket=t, direction=Message.DIRECTION_INBOUND,
                               from_email=email, subject="s", body_text="hi")
        return t

    def test_same_order_same_category_merges(self):
        first = self._ticket(thread="t1", order_id="DD9999")
        second = self._ticket(thread="t2", order_id="DD9999")  # new thread, same order
        surviving = service.merge_order_duplicate(second)
        self.assertEqual(surviving, first)
        self.assertFalse(Ticket.objects.filter(pk=second.pk).exists())
        self.assertEqual(first.messages.count(), 2)  # conversation appended
        self.assertTrue(first.audit_log.filter(event="conversation_appended").exists())

    def test_different_order_does_not_merge(self):
        self._ticket(thread="t1", order_id="DD1111")
        second = self._ticket(thread="t2", order_id="DD2222")
        self.assertIsNone(service.merge_order_duplicate(second))
        self.assertEqual(Ticket.objects.count(), 2)

    def test_different_category_does_not_merge(self):
        self._ticket(thread="t1", order_id="DD9999", category="1. Shipment")
        second = self._ticket(thread="t2", order_id="DD9999", category="7. Refund")
        self.assertIsNone(service.merge_order_duplicate(second))
        self.assertEqual(Ticket.objects.count(), 2)

    def test_closed_ticket_does_not_merge(self):
        self._ticket(thread="t1", order_id="DD9999", status=Ticket.STATUS_CLOSED)
        second = self._ticket(thread="t2", order_id="DD9999")
        self.assertIsNone(service.merge_order_duplicate(second))

    def test_no_order_id_does_not_merge(self):
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            thread_id="t9", customer_email="b@x.com", subject="hi", extracted={},
        )
        self.assertIsNone(service.merge_order_duplicate(t))


class MatchPriorityTests(TestCase):
    def setUp(self):
        from apps.taxonomy.models import Category, SubTopic
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.cat = Category.objects.create(brand=self.brand, code="1", name="Shipment")
        self.sub = SubTopic.objects.create(category=self.cat, code="1.1", name="Status")

    def _ticket(self, *, subject="hi", body="hi", order_id=None, category="1. Shipment",
                sub=True, email="b@x.com", status=Ticket.STATUS_AWAITING_AGENT):
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email=email, subject=subject, category=category,
            category_ref=self.cat, sub_topic_ref=self.sub if sub else None,
            status=status, extracted={"order_id": order_id} if order_id else {},
        )
        Message.objects.create(ticket=t, direction=Message.DIRECTION_INBOUND,
                               from_email=email, subject=subject, body_text=body)
        return t

    def test_match_by_ticket_id_mention(self):
        existing = self._ticket(subject="Order issue")
        new = self._ticket(subject="Re: " + existing.ticket_id,
                           body=f"Following up on {existing.ticket_id} please")
        surviving = service.match_and_merge(new)
        self.assertEqual(surviving, existing)
        self.assertFalse(Ticket.objects.filter(pk=new.pk).exists())

    def test_match_by_order_id(self):
        existing = self._ticket(order_id="DD9999")
        new = self._ticket(order_id="DD9999")
        self.assertEqual(service.match_and_merge(new), existing)

    def test_match_by_similarity_heuristic(self):
        # No AI in tests -> same category + same sub-topic merges (heuristic).
        existing = self._ticket(subject="Where is my order?")
        new = self._ticket(subject="Any update on my order?")
        self.assertEqual(service.match_and_merge(new), existing)

    def test_different_category_creates_new(self):
        from apps.taxonomy.models import Category, SubTopic
        cat3 = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        sub3 = SubTopic.objects.create(category=cat3, code="3.3", name="Damaged")
        self._ticket(subject="Where is my order?")  # category 1
        new = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="damaged product", category="3. Delivery Issues",
            category_ref=cat3, sub_topic_ref=sub3, status=Ticket.STATUS_AWAITING_AGENT,
        )
        Message.objects.create(ticket=new, direction=Message.DIRECTION_INBOUND,
                               from_email="b@x.com", subject="damaged", body_text="broken")
        self.assertIsNone(service.match_and_merge(new))  # different category -> new ticket

    def test_different_customer_creates_new(self):
        self._ticket(email="a@x.com")
        new = self._ticket(email="b@x.com")
        self.assertIsNone(service.match_and_merge(new))

    def _ticket_with_phone(self, phone, *, subject):
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="shared.sender@gmail.com",      # SAME sender across customers
            subject=subject, category="1. Shipment", category_ref=self.cat,
            sub_topic_ref=self.sub, status=Ticket.STATUS_AWAITING_AGENT,
            extracted={"phone": phone})
        Message.objects.create(ticket=t, direction=Message.DIRECTION_INBOUND,
                               from_email="shared.sender@gmail.com", subject=subject,
                               body_text=subject)
        return t

    def test_same_sender_different_verified_phone_creates_new(self):
        # The reported bug: one Gmail submits for TWO different verified customers (different
        # order-owner phones). Same sender + same issue must NOT merge -- different customer.
        self._ticket_with_phone("9999999999", subject="Where is my order?")
        new = self._ticket_with_phone("9983366901", subject="Any update on my order?")
        self.assertIsNone(service.match_and_merge(new))    # different verified phone -> NEW ticket

    def test_same_sender_same_verified_phone_still_merges(self):
        # Genuine follow-up: same sender AND same verified phone + same issue -> merge.
        existing = self._ticket_with_phone("9983366901", subject="Where is my order?")
        new = self._ticket_with_phone("9983366901", subject="Any update on my order?")
        self.assertEqual(service.match_and_merge(new), existing)

    def test_same_order_different_issue_creates_new_ticket(self):
        """#100 'Order Delayed' + later 'Wrong Item' on the SAME order -> #101, NOT
        appended to #100 (the reported 'compare issue type' improvement)."""
        from apps.taxonomy.models import SubTopic
        delayed = SubTopic.objects.create(category=self.cat, code="1.2", name="Order Delayed")
        wrong = SubTopic.objects.create(category=self.cat, code="1.3", name="Wrong Item")
        # #100: Order Delayed for order DD9999
        first = self._ticket(subject="order delayed", order_id="DD9999")
        first.sub_topic_ref = delayed
        first.save(update_fields=["sub_topic_ref"])
        # Later: Wrong Item for the SAME order DD9999, SAME category, DIFFERENT sub-topic
        second = self._ticket(subject="wrong item received", order_id="DD9999")
        second.sub_topic_ref = wrong
        second.save(update_fields=["sub_topic_ref"])
        self.assertIsNone(service.match_and_merge(second))   # NOT merged -> stays #101
        self.assertEqual(Ticket.objects.count(), 2)

    def test_same_order_same_issue_still_merges(self):
        """Same order + SAME sub-topic still merges (a genuine follow-up)."""
        first = self._ticket(subject="where is my order", order_id="DD9999")  # sub 1.1
        second = self._ticket(subject="any update on my order", order_id="DD9999")
        self.assertEqual(service.match_and_merge(second), first)


class _NoShop:
    """Shopify configured but returns NO match -> a cancellation identifier is 'not_found'."""
    def get_order(self, o):
        return None

    def recent_orders_by_phone(self, p, limit=5):
        return []

    def recent_orders_by_email(self, e, limit=5):
        return []


@override_settings(PUBLIC_BASE_URL="https://support.deodap.in")
class DuplicateProcessingTests(TestCase):
    """One incoming Gmail Message-ID -> processed and auto-replied EXACTLY once, across re-polls,
    concurrent workers, and pending replies. Our own outgoing support mail is ignored."""

    def setUp(self):
        from apps.brand_settings.models import BrandSettings, SupportEmail
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        SupportEmail.objects.create(brand=self.brand, email="care@deodap.com",
                                    is_active=True, is_primary=True)

    def _cancel_classify(self):
        return lambda b, m: ClassificationResult(
            category="6. Order Cancellation", sub_topic="6.1 Cancel", confidence=0.9,
            extracted={}, sentiment="neutral", language="en", is_support_request=True,
            issue_summary="cancel order", requires_evidence=False, requires_agent=False,
            category_ref=None, sub_topic_ref=None)

    def _patch(self, shopify=None):
        from apps.integrations import context as ctx
        self.sent = []
        self._ctx = ctx
        self._orig = (service._classify_dict, ctx.build_clients, service._send_customer_email)
        service._classify_dict = self._cancel_classify()
        ctx.build_clients = lambda s: {"shopify": shopify, "shipping": None, "gokwik": None}
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append(to) or "<sent-%d>" % len(self.sent))

    def _unpatch(self):
        service._classify_dict, self._ctx.build_clients, service._send_customer_email = self._orig

    def _msg(self, mid, body="I want to cancel my order.", from_email="buyer@example.com",
             subject="cancel order", in_reply_to=None, references=None):
        return {"message_id": mid, "gmail_message_id": mid, "thread_id": "TH-%s" % mid,
                "from_email": from_email, "to": "care@deodap.com", "subject": subject,
                "body_text": body, "body_html": "", "headers": {}, "attachments": [],
                "attachment_blobs": [], "in_reply_to": in_reply_to or "",
                "references": references or []}

    def _handle(self, msg):
        return service.handle_incoming_email(self.mailbox, dict(msg))

    # 1. One incoming email -> one auto reply.
    def test_one_email_one_reply(self):
        self._patch(shopify=_NoShop())
        try:
            self._handle(self._msg("<m1@x>"))
        finally:
            self._unpatch()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(ProcessedEmail.objects.filter(message_id="<m1@x>").count(), 1)

    # 2. Same email polled twice -> only one auto reply.
    def test_same_email_polled_twice(self):
        self._patch(shopify=_NoShop())
        try:
            m = self._msg("<m2@x>")
            self._handle(m)
            self._handle(m)                         # identical Message-ID re-delivered
        finally:
            self._unpatch()
        self.assertEqual(len(self.sent), 1)

    # 3. Two workers receive the same email -> only one auto reply (second skips safely).
    def test_two_workers_same_email(self):
        self._patch(shopify=_NoShop())
        try:
            m = self._msg("<m3@x>")
            self._handle(dict(m))                   # worker A
            r2 = self._handle(dict(m))              # worker B, same message
        finally:
            self._unpatch()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(r2, (None, None, False))   # skipped safely
        self.assertEqual(ProcessedEmail.objects.filter(message_id="<m3@x>").count(), 1)

    # 4. Support team's outgoing email is ignored (never processed, never claimed).
    def test_own_support_email_ignored(self):
        self._patch(shopify=_NoShop())
        try:
            self._handle(self._msg("<m4@x>", from_email="care@deodap.com"))
        finally:
            self._unpatch()
        self.assertEqual(len(self.sent), 0)
        self.assertFalse(ProcessedEmail.objects.filter(message_id="<m4@x>").exists())

    # 5. Pending verification does NOT send duplicate replies for the same incoming email.
    def test_pending_reply_not_duplicated(self):
        self._patch(shopify=_NoShop())
        try:
            self._handle(self._msg("<p0@x>"))       # first email -> pending + lookup ask (send 1)
            reply = self._msg("<p1@x>", body="my order id is 111111",
                              in_reply_to="<p0@x>", references=["<p0@x>"])
            self._handle(dict(reply))               # invalid order -> "couldn't find" (send 2)
            self._handle(dict(reply))               # SAME reply re-delivered -> deduped (no send)
        finally:
            self._unpatch()
        self.assertEqual(len(self.sent), 2)         # NOT 3 -- the re-delivered reply is skipped
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertEqual(PendingConversation.objects.count(), 1)   # no duplicate pending
