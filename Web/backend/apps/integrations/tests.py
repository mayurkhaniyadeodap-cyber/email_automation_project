"""
Offline tests for Phase 5 live-data integrations (doc sections 5 & 8).

Fake clients stand in for Shopify / Shipping / GoKwik (no network), so we test the
normalizers, the per-ticket context builder, the now fact-aware condition evaluator,
and the §7 worked examples that should AUTO-RESOLVE once live data is present.

    python manage.py test apps.integrations
"""

from django.test import TestCase

from apps.brand_settings.models import BrandSettings
from apps.decision import engine
from apps.decision.engine import evaluate_condition
from apps.integrations import context as live_context
from apps.integrations.clients import (
    GoKwikClient,
    ShippingClient,
    ShopifyClient,
    build_clients,
)
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import SubTopic
from apps.tickets.models import Message, Ticket


class NormalizeTests(TestCase):
    def test_shopify_order(self):
        order = ShopifyClient.normalize_order({
            "fulfillment_status": "fulfilled",
            "fulfillments": [{"tracking_url": "http://t/1", "tracking_number": "AWB1"}],
            "line_items": [{"product_id": 10}],
            "financial_status": "paid",
        })
        self.assertTrue(order["shipped"])
        self.assertTrue(order["dispatched"])
        self.assertEqual(order["tracking_url"], "http://t/1")
        self.assertEqual(order["awb"], "AWB1")
        self.assertFalse(order["custom_item"])

    def test_shopify_custom_item(self):
        order = ShopifyClient.normalize_order({
            "fulfillment_status": "unfulfilled",
            "line_items": [{"custom": True}],
        })
        self.assertTrue(order["custom_item"])
        self.assertFalse(order["shipped"])

    def test_customer_name_rejects_address_fragment(self):
        # The reported bug: the name field held an address ("1/166 - 42"); the REAL name lives in
        # the shipping address. Pick the real name, never the address fragment.
        order = ShopifyClient.normalize_order({
            "customer": {"first_name": "1/166", "last_name": "- 42"},
            "shipping_address": {"name": "G Chitra Dhavamuni"},
        })
        self.assertEqual(order["customer_name"], "G Chitra Dhavamuni")

    def test_customer_name_prefers_real_customer_name(self):
        order = ShopifyClient.normalize_order({
            "customer": {"first_name": "G Chitra", "last_name": "Dhavamuni"},
            "shipping_address": {"name": "1/166 - 42"},
        })
        self.assertEqual(order["customer_name"], "G Chitra Dhavamuni")

    def test_customer_name_blank_when_only_address_data(self):
        # No real name anywhere -> blank (-> "Unknown"), never an address fragment.
        order = ShopifyClient.normalize_order({
            "customer": {"first_name": "1/166", "last_name": "42"},
            "shipping_address": {"name": "12-A / 5"},
        })
        self.assertEqual(order["customer_name"], "")

    def test_phone_filter_drops_mismatched_order(self):
        # A fuzzy Shopify search returned a DIFFERENT customer's order (recipient 'Bitty') for a
        # number it doesn't really have -> must be dropped so the customer never sees a stranger's
        # shipment. The matching one is kept.
        from apps.integrations.clients import _orders_matching_phone
        wrong = {"order_id": "262282729", "customer_phone": "+918888000011", "customer_name": "Bitty"}
        right = {"order_id": "262300000", "customer_phone": "9991233655", "customer_name": "Me"}
        no_phone = {"order_id": "262300001", "customer_phone": "", "customer_name": "Guest"}
        kept = _orders_matching_phone([wrong, right, no_phone], "9991233655")
        ids = [o["order_id"] for o in kept]
        self.assertNotIn("262282729", ids)        # stranger's order dropped
        self.assertIn("262300000", ids)           # exact phone match kept
        self.assertIn("262300001", ids)           # no-phone order kept (can't disprove)

    def test_phone_filter_handles_e164_and_prefixes(self):
        from apps.integrations.clients import _orders_matching_phone
        orders = [{"order_id": "1", "customer_phone": "+91 99912 33655"},
                  {"order_id": "2", "customer_phone": "09991233655"}]
        kept = _orders_matching_phone(orders, "9991233655")
        self.assertEqual({o["order_id"] for o in kept}, {"1", "2"})

    def test_shipping_tracking(self):
        t = ShippingClient.normalize_tracking(
            {"status": "Delivered", "edd": "2026-06-01", "tracking_url": "http://t/2"}
        )
        self.assertEqual(t["status"], "delivered")
        self.assertTrue(t["delivered"])
        self.assertTrue(t["shipped"])

    def test_gokwik_double_payment(self):
        p = GoKwikClient.normalize_payment({"payments": [
            {"status": "captured", "amount": 100},
            {"status": "captured", "amount": 100},
        ]})
        self.assertTrue(p["double_payment"])
        self.assertTrue(p["paid"])
        self.assertEqual(p["amount"], 200)

    def test_build_clients_from_settings(self):
        org = Organization.objects.create(name="O")
        brand = Brand.objects.create(organization=org, name="B")
        s = BrandSettings.objects.create(brand=brand, integrations={
            "shopify": {"shop": "x.myshopify.com", "token": "t"},
        })
        clients = build_clients(s)
        self.assertIsInstance(clients["shopify"], ShopifyClient)
        self.assertIsNone(clients["shipping"])
        self.assertIsNone(clients["gokwik"])


class ConditionWithFactsTests(TestCase):
    def test_shipped_not_breached(self):
        self.assertIs(
            evaluate_condition("Order shipped AND EDD not breached",
                               {"shipped": True, "edd_breached": False}), True)

    def test_shipped_breached_false_when_not_breached(self):
        self.assertIs(
            evaluate_condition("Order shipped AND EDD not breached",
                               {"shipped": True, "edd_breached": True}), False)

    def test_edd_breached_clause(self):
        self.assertIs(
            evaluate_condition("Shipped AND EDD breached",
                               {"shipped": True, "edd_breached": True}), True)

    def test_not_dispatched_not_custom(self):
        self.assertIs(
            evaluate_condition("Not dispatched AND not a custom item",
                               {"dispatched": False, "custom_item": False}), True)
        self.assertIs(
            evaluate_condition("Not dispatched AND not a custom item",
                               {"dispatched": True, "custom_item": False}), False)

    def test_partial_facts_unevaluable(self):
        self.assertIsNone(
            evaluate_condition("Order shipped AND EDD not breached", {"shipped": True}))

    def test_delivered_clause(self):
        self.assertIs(
            evaluate_condition("Marked delivered but customer reports not received",
                               {"delivered": True}), True)


class FakeShopify:
    def __init__(self, order):
        self.order = order

    def get_order(self, order_id):
        return self.order


class FakeShipping:
    def __init__(self, tracking):
        self.tracking = tracking

    def track(self, awb):
        return self.tracking


class ContextBuilderTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand)

    def _ticket(self, extracted):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="x", extracted=extracted,
        )

    def test_context_merges_shopify_and_computes_edd_breached(self):
        ticket = self._ticket({"order_id": "DD1"})
        clients = {
            "shopify": FakeShopify({
                "shipped": True, "dispatched": True, "delivered": False,
                "edd": "2020-01-01", "tracking_url": "http://t/9",
                "custom_item": False, "awb": "AWB9",
            }),
            "shipping": None, "gokwik": None,
        }
        facts = live_context.build_context(ticket, clients=clients)
        self.assertTrue(facts["shipped"])
        self.assertEqual(facts["tracking_url"], "http://t/9")
        self.assertTrue(facts["edd_breached"])  # 2020 is in the past

    def test_shipping_overrides_delivered(self):
        ticket = self._ticket({"order_id": "DD1", "awb": "AWB9"})
        clients = {
            "shopify": None,
            "shipping": FakeShipping({"status": "delivered", "delivered": True,
                                      "shipped": True, "edd": "", "tracking_url": ""}),
            "gokwik": None,
        }
        facts = live_context.build_context(ticket, clients=clients)
        self.assertTrue(facts["delivered"])

    def test_no_clients_empty_facts(self):
        ticket = self._ticket({"order_id": "DD1"})
        facts = live_context.build_context(
            ticket, clients={"shopify": None, "shipping": None, "gokwik": None})
        self.assertEqual(facts, {})


class WorkedExampleTests(TestCase):
    """The doc §7 examples that auto-resolve once live data is wired in."""

    def setUp(self):
        from django.core.management import call_command

        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, confidence_threshold=0.75)
        call_command("seed_taxonomy", brand=self.brand.id)

    def _sub(self, code):
        return SubTopic.objects.get(category__brand=self.brand, code=code)

    def _ticket(self, code, extracted, sentiment="neutral"):
        sub = self._sub(code)
        ticket = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="help", sub_topic_ref=sub,
            category_ref=sub.category, status=Ticket.STATUS_CLASSIFIED,
            classification_status=Ticket.CLS_CLASSIFIED,
            ai_confidence=0.9, sentiment=sentiment, extracted=extracted,
            mandatory_inputs=sub.mandatory_inputs,
        )
        Message.objects.create(ticket=ticket, direction=Message.DIRECTION_INBOUND,
                               from_email="b@x.com", subject="help", body_text="?")
        return ticket

    def test_where_is_my_order_auto_resolves(self):
        ticket = self._ticket("1.1", {"order_id": "DD123"})
        facts = {"shipped": True, "edd_breached": False,
                 "tracking_url": "http://t/123", "edd": "2026-12-31"}
        engine.run(ticket, context=facts)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_AUTO_RESOLVED)
        sent = ticket.messages.get(direction=Message.DIRECTION_OUTBOUND)
        self.assertFalse(sent.is_draft)
        self.assertIn("http://t/123", sent.body_text)

    def test_delayed_order_drafts_for_agent(self):
        ticket = self._ticket("1.3", {"order_id": "DD123"})
        facts = {"shipped": True, "edd_breached": True}
        plan = engine.run(ticket, context=facts)
        self.assertEqual(plan.action_code, "create_ticket")
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_AWAITING_AGENT)

    def test_cancel_not_shipped_triggers_crp(self):
        ticket = self._ticket("6.1", {"order_id": "DD123"})
        facts = {"dispatched": False, "custom_item": False}
        plan = engine.run(ticket, context=facts)
        self.assertEqual(plan.action_code, "trigger_cancellation_refund_pickup")
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_ESCALATED)
        self.assertEqual(ticket.priority, Ticket.PRIORITY_HIGH)

    def test_without_live_data_does_not_send_placeholder_or_ticket(self):
        ticket = self._ticket("1.1", {"order_id": "DD123"})
        plan = engine.run(ticket, context={})  # no live facts, no tracking_url injected
        # Shipment Tracking (cat 1) is a NO_TICKET info category. A half-filled template
        # ({tracking_url} the lookup couldn't fill) must NEVER be sent verbatim and must
        # not draft into a ticket: the case stays in the auto-reply flow, and without an
        # AI responder (test mode blanks keys) it falls back to an agent -- not a draft of
        # the raw placeholder.
        self.assertNotIn("unresolved_placeholders", plan.reasons)
        self.assertIn("policy_auto_reply", plan.reasons)
        self.assertIn("no_auto_answer", plan.reasons)
        out = ticket.messages.filter(direction=Message.DIRECTION_OUTBOUND).first()
        if out:
            self.assertNotIn("{tracking_url}", out.body_text)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_AWAITING_AGENT)


class PhoneLookupVariantTests(TestCase):
    """recent_orders_by_phone must find a customer whose phone Shopify STORED in E.164
    (+91...) even when the customer typed a bare 10-digit number (the verification bug)."""

    PHONE = "7004810519"

    def _client(self, stored_format):
        from apps.integrations import clients as cl

        calls = {"phone_queries": []}

        class _Resp:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class _FakeRequests:
            def get(_self, url, headers=None, params=None, timeout=None):
                params = params or {}
                if "customers/search" in url:
                    q = params.get("query", "")
                    calls["phone_queries"].append(q)
                    # Shopify only matches when the QUERIED token equals the STORED format.
                    if q == f"phone:{stored_format}":
                        return _Resp({"customers": [{"id": 555}]})
                    return _Resp({"customers": []})
                # orders for the matched customer
                return _Resp({"orders": [{"name": "#262339239", "fulfillment_status": "fulfilled",
                                          "customer": {"first_name": "Raneesh",
                                                       "last_name": "K"}}]})

        orig = cl._requests
        cl._requests = lambda: _FakeRequests()
        self.addCleanup(lambda: setattr(cl, "_requests", orig))
        return ShopifyClient("shop.myshopify.com", "tok", "2024-10"), calls

    def test_bare_query_matches_e164_stored(self):
        client, calls = self._client(stored_format="+917004810519")
        orders = client.recent_orders_by_phone(self.PHONE)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["order_id"], "#262339239")
        # It tried the bare form first, then the +91 form that matched.
        self.assertIn("phone:7004810519", calls["phone_queries"])
        self.assertIn("phone:+917004810519", calls["phone_queries"])

    def test_bare_query_matches_bare_stored(self):
        client, _ = self._client(stored_format="7004810519")
        self.assertEqual(len(client.recent_orders_by_phone(self.PHONE)), 1)

    def test_no_customer_returns_empty(self):
        client, _ = self._client(stored_format="9999999999")
        self.assertEqual(client.recent_orders_by_phone(self.PHONE), [])

    def test_guest_order_found_via_order_search(self):
        # GUEST / COD order: NO customer record (customers/search empty), but the ORDER search
        # by phone finds it -> verification succeeds and the order owner's name is resolved
        # (the reported "valid number won't verify + Customer Name: Unknown" bug).
        from apps.integrations import clients as cl

        class _Resp:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        posts = []

        class _FakeRequests:
            def get(_self, url, headers=None, params=None, timeout=None):
                if "customers/search" in url:
                    return _Resp({"customers": []})            # no customer for any variant
                return _Resp({"orders": [{                     # REST fetch by name
                    "name": "#577481", "fulfillment_status": "fulfilled",
                    "customer": {"first_name": "Nandakumar", "last_name": "K N"},
                    "shipping_address": {"phone": "+919895798462"}}]})

            def post(_self, url, headers=None, json=None, timeout=None):
                posts.append(json)
                assert "graphql.json" in url
                return _Resp({"data": {"orders": {"edges": [{"node": {"name": "#577481"}}]}}})

        orig = cl._requests
        cl._requests = lambda: _FakeRequests()
        self.addCleanup(lambda: setattr(cl, "_requests", orig))
        client = ShopifyClient("shop.myshopify.com", "tok", "2024-10")
        orders = client.recent_orders_by_phone("9895798462")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["order_id"], "#577481")
        self.assertEqual(orders[0]["customer_name"], "Nandakumar K N")     # not "Unknown"
        self.assertTrue(posts and "phone:9895798462" in posts[0]["variables"]["q"])
