"""
Regression: a Shipment-Tracking conversation must NOT create duplicate tickets when the
customer replies in steps (phone first, then order number), and must stay in the
auto-reply (no-ticket) flow.

Reported bug: "where is my order" -> reply "Phone: 45678912352" -> reply "Order: 456753"
created TWO tickets (TKT-2026-000122 + 000123) and no tracking link.

    python manage.py test apps.ingestion.tests_tracking_pending
"""

from django.test import TestCase, override_settings

from apps.classifier.service import ClassificationResult
from apps.ingestion import service
from apps.ingestion.tests_imap import FakeImap
from apps.ingestion.tests_smart import eml
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, Rule, SubTopic
from apps.tickets.models import Message, PendingConversation, Ticket


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class TrackingPendingNoDuplicateTests(TestCase):
    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        self.cat = Category.objects.create(brand=self.brand, code="1",
                                           name="Shipment & Delivery Tracking")
        self.sub = SubTopic.objects.create(category=self.cat, code="1.1",
                                            name="Shipment Status", mandatory_inputs=["order_id"])
        # A clean info_only answer (no live placeholder) -> auto-resolves once order known.
        Rule.objects.create(sub_topic=self.sub, condition="Always", action=Rule.ACTION_INFO_ONLY,
                            then_response="Your order {order_id} is on its way.")

    def _result(self):
        return ClassificationResult(
            category="1. Shipment & Delivery Tracking", sub_topic="1.1 Shipment Status",
            confidence=0.9, extracted={}, sentiment="neutral", language="en",
            is_support_request=True, issue_summary="where is my order",
            requires_evidence=False, requires_agent=False,
            category_ref=self.cat, sub_topic_ref=self.sub)

    def _run(self, *emails):
        from apps.integrations import identity as idmod
        orig_cls, orig_resolve = service._classify_dict, idmod.resolve_identity
        service._classify_dict = lambda b, m: self._result()
        # Shopify "configured but order not found" -> original email is held + M1 sent.
        idmod.resolve_identity = lambda *a, **k: {
            "order": None, "orders": [], "needs_choice": False,
            "source": "none", "configured": True}
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            service._classify_dict, idmod.resolve_identity = orig_cls, orig_resolve

    def test_phone_then_order_creates_no_duplicate_and_no_ticket(self):
        self._run(
            eml(subject="where is my order", body="Hi, where is my order?", message_id="<a@x>"),
            # Reply 1: only a phone (and an 11-digit one) -> must NOT promote to a ticket.
            eml(subject="Re: where is my order", body="PHONE NUMBER : 45678912352",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>"),
            # Reply 2: the order number -> lookup + auto-reply, still NO ticket.
            eml(subject="Re: where is my order", body="order number : 456753",
                message_id="<a3@x>", in_reply_to="<a@x>", references="<a@x>"),
        )

        # The phone-labelled 11-digit number is NOT an order id.
        from apps.classifier.rule_classifier import _extract_order_id
        self.assertIsNone(_extract_order_id("PHONE NUMBER : 45678912352"))

        # Shipment Tracking is an auto-reply category -> NO ticket at all (not even one),
        # and certainly never two.
        self.assertEqual(Ticket.objects.filter(status=Ticket.STATUS_AWAITING_AGENT).count(), 0)
        autoresolved = Ticket.objects.filter(status=Ticket.STATUS_AUTO_RESOLVED)
        self.assertLessEqual(autoresolved.count(), 1)            # at most one, auto-resolved
        self.assertEqual(Ticket.objects.count(), autoresolved.count())  # no extra/duplicate

        # No "Support Ticket Created Successfully" (M5) was ever sent for tracking.
        self.assertFalse(Message.objects.filter(
            direction=Message.DIRECTION_OUTBOUND,
            subject="Support Ticket Created Successfully").exists())

    def test_phone_reply_keeps_pending_open(self):
        # After the original + a phone-only reply, the case is STILL pending (re-asked for
        # the order) -- it was not promoted into a ticket.
        self._run(
            eml(subject="where is my order", body="where is my order?", message_id="<a@x>"),
            eml(subject="Re: where is my order", body="PHONE NUMBER : 45678912352",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>"),
        )
        self.assertEqual(Ticket.objects.count(), 0)              # no ticket yet
        p = PendingConversation.objects.get()                   # exactly one pending
        self.assertEqual(p.order_id or "", "")                  # order still not captured
        self.assertGreaterEqual(p.evidence_requests, 2)         # asked for order again
