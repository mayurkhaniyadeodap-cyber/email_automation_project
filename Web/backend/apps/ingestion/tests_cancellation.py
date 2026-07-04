"""
Order-cancellation flow (reported bug: "cancel my order. my mobile is 9907465210"
was routed into the DAMAGE evidence workflow and the phone read as an order id).

    python manage.py test apps.ingestion.tests_cancellation
"""

from django.test import TestCase, override_settings

from apps.ingestion import evidence, service
from apps.ingestion.tests_imap import FakeImap
from apps.ingestion.tests_smart import eml
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import PendingConversation, Ticket


class CancellationPolicyTests(TestCase):
    def test_is_cancellation(self):
        for t in ["I want to cancel my order", "please cancel order", "order cancellation",
                  "cancel my order. my mobile is 9907465210", "I wish to cancel"]:
            self.assertTrue(evidence.is_cancellation(t), t)
        for t in ["my product is damaged", "where is my order", "wrong item"]:
            self.assertFalse(evidence.is_cancellation(t), t)

    def test_cancellation_beats_damage_keyword(self):
        # "cancel my damaged order" -> CANCELLATION, NOT a photo/video workflow.
        self.assertEqual(
            evidence.evidence_level(text="I want to cancel my damaged order",
                                    issue_summary="damaged"),
            evidence.EV_NONE)

    def test_damage_still_requires_photo(self):
        self.assertEqual(evidence.evidence_level(text="my product is damaged",
                                                 issue_summary="damaged product"),
                         evidence.EV_PHOTO)


@override_settings(PUBLIC_BASE_URL="https://support.deodap.in")
class CancellationFlowTests(TestCase):
    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)

    def _provider(self, *, damaged=False):
        import json as _json
        # Even if the AI says DAMAGED, cancellation detection must win for cancel text.
        cat = ("3. Delivery Issues", "3.3 Damaged") if damaged else ("6. Order Cancellation", "6.1 Cancel")

        class FP:
            def generate(self, system, user):
                return _json.dumps({
                    "is_support_request": True, "category": cat[0], "sub_topic": cat[1],
                    "confidence": 0.9, "requires_evidence": damaged, "requires_agent": False,
                    "issue_summary": "damaged" if damaged else "cancel order",
                    "sentiment": "neutral", "extracted": {}})
        return FP()

    def _run(self, provider, emails):
        from apps.classifier import service as classifier
        orig = classifier.build_provider
        classifier.build_provider = lambda s: provider
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            classifier.build_provider = orig

    # --- Case 1: cancellation -------------------------------------------------------
    def test_cancel_intent_phone_extracted_no_evidence(self):
        # Provider deliberately returns DAMAGED to prove cancellation overrides it.
        self._run(self._provider(damaged=True), [
            eml(subject="cancel order",
                body="I want to cancel my order. My mobile number is 9907465210.",
                message_id="<a@x>")])
        self.assertEqual(Ticket.objects.count(), 0)                # no ticket yet
        p = PendingConversation.objects.get()
        self.assertEqual(p.extracted.get("intent"), "ORDER_CANCELLATION")  # intent
        self.assertEqual(p.phone, "9907465210")                    # phone extracted
        self.assertEqual(p.order_id, "")                           # phone NOT used as order
        self.assertFalse(p.requires_evidence)                      # no evidence
        self.assertNotEqual(p.status, "waiting_for_video")         # NOT the damage path
        self.assertEqual(_pending_level(p), evidence.EV_NONE)      # never asks for photo/video
        self.assertGreaterEqual(p.evidence_requests, 1)            # M_CANCEL_LOOKUP sent

    def test_cancel_with_order_in_reply_creates_ticket(self):
        self._run(self._provider(damaged=True), [
            eml(subject="cancel order", body="I want to cancel my order, mobile 9907465210",
                message_id="<a@x>"),
            eml(subject="Re: cancel order", body="my order id is 9027510",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>")])
        self.assertEqual(Ticket.objects.count(), 1)                # created on the order reply
        self.assertEqual(Ticket.objects.get().extracted.get("order_id"), "9027510")

    # --- Case 2: damage still works -------------------------------------------------
    def test_damage_still_asks_for_photo(self):
        self._run(self._provider(damaged=True), [
            eml(subject="my product is damage",
                body="My product is damaged, my mobile is 8765321519", message_id="<a@x>")])
        self.assertEqual(Ticket.objects.count(), 0)
        p = PendingConversation.objects.get()
        self.assertNotEqual(p.extracted.get("intent"), "ORDER_CANCELLATION")
        self.assertEqual(_pending_level(p), evidence.EV_PHOTO)      # generic level: photo floor
        # NEW rule: Damaged now requires BOTH a photo AND a video (EV_DAMAGED), so the
        # delivered-item gate holds the conversation in 'waiting_for_video' (the video-mandatory
        # wait state), never the photo-only 'awaiting_evidence'.
        self.assertEqual(p.status, "waiting_for_video")


def _pending_level(p):
    return service._pending_evidence_level(p)


class _CancelFakeShopify:
    def __init__(self, orders=None, by_phone=None, by_email=None):
        self.orders = orders or {}
        self.by_phone = by_phone or {}
        self.by_email = by_email or {}

    def get_order(self, order_id):
        return self.orders.get(order_id)

    def recent_orders_by_phone(self, phone, limit=5):
        return self.by_phone.get(phone, [])

    def recent_orders_by_email(self, email, limit=5):
        return self.by_email.get(email, [])


class _CancelFakeShipping:
    def __init__(self, by_awb):
        self.by_awb = by_awb

    def track(self, awb):
        return self.by_awb.get(awb)


_CANCEL_ORDER = {"order_id": "9027510", "shipped": True, "delivered": False,
                 "raw_fulfillment_status": "In Transit", "customer_name": "Test Buyer",
                 "customer_email": "buyer@shop.com", "awb": "", "courier": ""}
_CANCEL_TRACK = {"status": "in_transit", "raw_status": "In Transit", "delivered": False,
                 "shipped": True, "courier": "DTDC", "awb": "7D132828320"}


@override_settings(PUBLIC_BASE_URL="https://support.deodap.in")
class CancellationVerificationTests(TestCase):
    """Regression: a cancellation identifier MUST verify (Shopify order/email or courier AWB)
    before ANY ticket is created. A bare / invalid value must never create a ticket."""

    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)

    def _classify(self):
        from apps.classifier.service import ClassificationResult
        return lambda b, m: ClassificationResult(
            category="6. Order Cancellation", sub_topic="6.1 Cancel", confidence=0.9,
            extracted={}, sentiment="neutral", language="en", is_support_request=True,
            issue_summary="cancel order", requires_evidence=False, requires_agent=False,
            category_ref=None, sub_topic_ref=None)

    def _run(self, reply_body, *, shopify=None, shipping=None):
        from apps.integrations import context as ctx
        self.sent = []
        clients = {"shopify": shopify, "shipping": shipping, "gokwik": None}
        oc, ob, oe = service._classify_dict, ctx.build_clients, service._send_customer_email
        service._classify_dict = self._classify()
        ctx.build_clients = lambda settings: clients
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body}) or "<sent>")
        try:
            service.fetch_imap(self.mailbox, client=FakeImap([eml(
                subject="cancel order", body="I want to cancel my order.",
                message_id="<a@x>")], start_uid=1))
            service.fetch_imap(self.mailbox, client=FakeImap([eml(
                subject="Re: cancel order", body=reply_body, message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>")], start_uid=2))
        finally:
            service._classify_dict, ctx.build_clients, service._send_customer_email = oc, ob, oe

    def _last_body(self):
        return (self.sent[-1]["body"] if self.sent else "").lower()

    # 1. Valid Order Number -> ticket created.
    def test_valid_order_creates_ticket(self):
        self._run("my order id is 9027510",
                  shopify=_CancelFakeShopify(orders={"9027510": dict(_CANCEL_ORDER)}))
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Ticket.objects.get().extracted.get("order_id"), "9027510")

    # 2. Invalid Order Number -> NO ticket (the reported bug).
    def test_invalid_order_no_ticket(self):
        self._run("64324544446", shopify=_CancelFakeShopify(orders={}))   # configured, no match
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertIn("couldn't find", self._last_body())

    # 3. Valid AWB -> ticket created.
    def test_valid_awb_creates_ticket(self):
        self._run("AWB 7D132828320", shopify=_CancelFakeShopify(orders={}),
                  shipping=_CancelFakeShipping({"7D132828320": dict(_CANCEL_TRACK)}))
        self.assertEqual(Ticket.objects.count(), 1)

    # 4. Invalid AWB -> NO ticket.
    def test_invalid_awb_no_ticket(self):
        self._run("AWB 7D999999999", shopify=_CancelFakeShopify(orders={}),
                  shipping=_CancelFakeShipping({}))          # courier doesn't know it
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertIn("couldn't find", self._last_body())

    # 5. Valid Registered Email -> ticket created.
    def test_valid_email_creates_ticket(self):
        self._run("my registered email is buyer@shop.com",
                  shopify=_CancelFakeShopify(by_email={"buyer@shop.com": [dict(_CANCEL_ORDER)]}))
        self.assertEqual(Ticket.objects.count(), 1)

    # 6. Invalid Registered Email -> NO ticket.
    def test_invalid_email_no_ticket(self):
        self._run("my registered email is nobody@shop.com",
                  shopify=_CancelFakeShopify(by_email={}))   # configured, no match
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertIn("couldn't find", self._last_body())


@override_settings(PUBLIC_BASE_URL="https://support.deodap.in")
class CancellationRetryTests(TestCase):
    """The pending-cancellation UPDATE workflow: every reply is re-parsed and the NEWEST identifier
    replaces the previous one and is re-verified fresh. A valid reply after an invalid one must
    create the ticket -- no stale re-validation, no restart, no duplicate pending."""

    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)

    def _classify(self):
        from apps.classifier.service import ClassificationResult
        return lambda b, m: ClassificationResult(
            category="6. Order Cancellation", sub_topic="6.1 Cancel", confidence=0.9,
            extracted={}, sentiment="neutral", language="en", is_support_request=True,
            issue_summary="cancel order", requires_evidence=False, requires_agent=False,
            category_ref=None, sub_topic_ref=None)

    def _run(self, *reply_bodies, shopify=None, shipping=None):
        from apps.integrations import context as ctx
        self.sent = []
        clients = {"shopify": shopify, "shipping": shipping, "gokwik": None}
        oc, ob, oe = service._classify_dict, ctx.build_clients, service._send_customer_email
        service._classify_dict = self._classify()
        ctx.build_clients = lambda settings: clients
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body}) or "<sent>")
        try:
            service.fetch_imap(self.mailbox, client=FakeImap([eml(
                subject="cancel order", body="I want to cancel my order.",
                message_id="<a0@x>")], start_uid=1))
            for i, body in enumerate(reply_bodies, start=1):
                service.fetch_imap(self.mailbox, client=FakeImap([eml(
                    subject="Re: cancel order", body=body, message_id="<a%d@x>" % i,
                    in_reply_to="<a0@x>", references="<a0@x>")], start_uid=i + 1))
        finally:
            service._classify_dict, ctx.build_clients, service._send_customer_email = oc, ob, oe

    def _last_body(self):
        return (self.sent[-1]["body"] if self.sent else "").lower()

    # 1. Invalid Order -> Valid Order -> Ticket
    def test_invalid_then_valid_order(self):
        self._run("my order id is 111111", "my order id is 9027510",
                  shopify=_CancelFakeShopify(orders={"9027510": dict(_CANCEL_ORDER)}))
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(PendingConversation.objects.count(), 0)   # pending consumed / cleared

    # 2. Invalid Order -> Mobile number -> Ticket
    def test_invalid_then_mobile(self):
        self._run("my order id is 111111", "my mobile is 9876543210",
                  shopify=_CancelFakeShopify(by_phone={"9876543210": [dict(_CANCEL_ORDER)]}))
        self.assertEqual(Ticket.objects.count(), 1)

    # 3. Invalid Order -> Valid AWB -> Ticket
    def test_invalid_then_awb(self):
        self._run("my order id is 111111", "AWB 7D132828320",
                  shopify=_CancelFakeShopify(orders={}),
                  shipping=_CancelFakeShipping({"7D132828320": dict(_CANCEL_TRACK)}))
        self.assertEqual(Ticket.objects.count(), 1)

    # 4. Invalid Order -> Valid Registered Email -> Ticket
    def test_invalid_then_email(self):
        self._run("my order id is 111111", "my email is buyer@shop.com",
                  shopify=_CancelFakeShopify(by_email={"buyer@shop.com": [dict(_CANCEL_ORDER)]}))
        self.assertEqual(Ticket.objects.count(), 1)

    # 5. Invalid Order -> Invalid Order -> Still Pending
    def test_invalid_then_invalid(self):
        self._run("my order id is 111111", "my order id is 222222",
                  shopify=_CancelFakeShopify(orders={}))
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertEqual(PendingConversation.objects.count(), 1)
        self.assertIn("couldn't find", self._last_body())

    # 6. Multiple invalid attempts -> final valid -> Ticket
    def test_multiple_invalid_then_valid(self):
        self._run("111111", "222222", "333333", "my order id is 9027510",
                  shopify=_CancelFakeShopify(orders={"9027510": dict(_CANCEL_ORDER)}))
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(PendingConversation.objects.count(), 0)   # cleared on success

    # 7. The latest identifier always REPLACES the previous one (no stale re-validation).
    def test_latest_identifier_replaces_previous(self):
        self._run("order 111111", "order 222222",
                  shopify=_CancelFakeShopify(orders={}))
        self.assertEqual(Ticket.objects.count(), 0)
        p = PendingConversation.objects.get()
        self.assertEqual(p.order_id, "222222")    # newest value stored, previous discarded
