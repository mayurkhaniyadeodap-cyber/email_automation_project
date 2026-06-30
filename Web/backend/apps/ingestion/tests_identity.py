"""
Tests for self-lookup / identity resolution (Mail Flow §3b/§6) and the M1 mail.

    python manage.py test apps.ingestion.tests_identity
"""

from django.test import TestCase

from apps.classifier.service import ClassificationResult
from apps.integrations import identity
from apps.ingestion import service
from apps.ingestion.tests_imap import FakeImap
from apps.ingestion.tests_smart import eml
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, SubTopic
from apps.tickets.models import PendingConversation, Ticket


def order(name, **extra):
    base = {"name": name, "order_id": name, "shipped": True, "edd": "2026-06-20"}
    base.update(extra)
    return base


class FakeShopify:
    def __init__(self, *, by_order=None, by_email=None, by_phone=None):
        self.by_order = by_order or {}
        self.by_email = by_email or {}
        self.by_phone = by_phone or {}

    def get_order(self, order_id):
        return self.by_order.get(order_id)

    def recent_orders_by_email(self, email, limit=5):
        return self.by_email.get(email, [])

    def recent_orders_by_phone(self, phone, limit=5):
        return self.by_phone.get(phone, [])


class ResolveIdentityTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")

    def _msg(self, **kw):
        kw.setdefault("from_email", "buyer@example.com")
        kw.setdefault("subject", "where is my order")
        kw.setdefault("body_text", "any update?")
        return kw

    def test_not_configured_returns_configured_false(self):
        out = identity.resolve_identity(self.brand, self._msg(), clients={"shopify": None})
        self.assertFalse(out["configured"])
        self.assertIsNone(out["order"])

    def test_resolves_by_order_id_in_body(self):
        clients = {"shopify": FakeShopify(by_order={"DD9999": order("DD9999")})}
        out = identity.resolve_identity(
            self.brand, self._msg(body_text="my order DD9999 please"), clients=clients)
        self.assertEqual(out["source"], "order_id")
        self.assertEqual(out["order"]["order_id"], "DD9999")

    def test_single_email_order_auto_selected(self):
        clients = {"shopify": FakeShopify(by_email={"buyer@example.com": [order("DD1001")]})}
        out = identity.resolve_identity(self.brand, self._msg(), clients=clients)
        self.assertEqual(out["source"], "email")
        self.assertFalse(out["needs_choice"])
        self.assertEqual(out["order"]["order_id"], "DD1001")

    def test_multiple_email_orders_need_choice(self):
        clients = {"shopify": FakeShopify(
            by_email={"buyer@example.com": [order("DD1001"), order("DD1002")]})}
        out = identity.resolve_identity(self.brand, self._msg(), clients=clients)
        self.assertTrue(out["needs_choice"])
        self.assertIsNone(out["order"])
        self.assertEqual(len(out["orders"]), 2)

    def test_phone_fallback_when_no_email_match(self):
        clients = {"shopify": FakeShopify(
            by_email={}, by_phone={"9876543210": [order("DD2002")]})}
        out = identity.resolve_identity(
            self.brand, self._msg(body_text="call me 9876543210"), clients=clients)
        self.assertEqual(out["source"], "phone")
        self.assertEqual(out["order"]["order_id"], "DD2002")


class SelfLookupFlowTests(TestCase):
    """End-to-end: a support mail with no order number, resolved (or not) via Shopify."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        self.sub = SubTopic.objects.create(category=self.cat, code="3.3", name="Damaged",
                                            requires_evidence=True, mandatory_inputs=["order_id"])

    def _patch_classify(self, monkey, *, requires_evidence=True):
        result = ClassificationResult(
            category="3. Delivery Issues", sub_topic="3.3 Damaged", confidence=0.9,
            extracted={}, sentiment="neutral", language="en", is_support_request=True,
            issue_summary="damaged", requires_evidence=requires_evidence,
            requires_agent=False, category_ref=self.cat, sub_topic_ref=self.sub)
        monkey(result)
        return result

    def test_m1_sent_when_no_identity_and_order_needed(self):
        orig = service._classify_dict
        service._classify_dict = lambda b, m: self._patch_classify(lambda r: None)
        orig_id = service._resolve_identity_or_request
        # Force "Shopify configured but nothing found".
        from apps.integrations import identity as idmod
        orig_resolve = idmod.resolve_identity
        idmod.resolve_identity = lambda *a, **k: {
            "order": None, "orders": [], "needs_choice": False,
            "source": "none", "configured": True}
        try:
            service.fetch_imap(self.mailbox, client=FakeImap([
                eml(subject="my product is damaged", body="it broke, no order id",
                    message_id="<a@x>")]))
        finally:
            service._classify_dict = orig
            idmod.resolve_identity = orig_resolve

        self.assertEqual(Ticket.objects.count(), 0)             # held, no ticket
        p = PendingConversation.objects.get()
        self.assertGreaterEqual(p.evidence_requests, 1)         # M1 sent

    def test_single_order_auto_adopted_then_normal_flow(self):
        orig = service._classify_dict
        service._classify_dict = lambda b, m: self._patch_classify(lambda r: None)
        from apps.integrations import identity as idmod
        orig_resolve = idmod.resolve_identity
        idmod.resolve_identity = lambda *a, **k: {
            "order": {"order_id": "DD7777", "name": "DD7777"}, "orders": [{}],
            "needs_choice": False, "source": "email", "configured": True}
        try:
            service.fetch_imap(self.mailbox, client=FakeImap([
                eml(subject="my product is damaged", body="broke, here", message_id="<a@x>")]))
        finally:
            service._classify_dict = orig
            idmod.resolve_identity = orig_resolve

        # Order adopted -> the pending now carries DD7777 (no M1 needed for the order).
        p = PendingConversation.objects.get()
        self.assertEqual(p.order_id, "DD7777")
