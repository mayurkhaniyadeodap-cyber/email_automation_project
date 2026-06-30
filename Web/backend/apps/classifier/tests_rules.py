"""
Tests for the keyword rule-based classifier fallback (no AI key / quota needed).

    python manage.py test apps.classifier.tests_rules
"""

from django.test import TestCase, override_settings

from apps.brand_settings.models import BrandSettings
from apps.classifier import rule_classifier
from apps.classifier import service as classifier
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, SubTopic
from apps.tickets.models import Message, Ticket


class BaseFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        # Minimal taxonomy for a few categories the keywords map to.
        for code, name in [("1", "Shipment & Delivery Tracking"),
                           ("3", "Delivery Issues (Post-Delivery)"),
                           ("7", "Return, Refund & Replacement")]:
            cat = Category.objects.create(brand=self.brand, code=code, name=name)
            SubTopic.objects.create(category=cat, code=f"{code}.1", name="General")


class RuleClassifierTests(BaseFixture):
    def test_shipment_keywords(self):
        d = rule_classifier.build_data(self.brand, {"subject": "Where is my order DD9999?",
                                                    "body_text": "when will it arrive"})
        self.assertTrue(d["category"].startswith("1."))
        self.assertEqual(d["extracted"]["order_id"], "DD9999")
        self.assertEqual(d["action"], "auto_reply")

    def test_refund_routes_to_agent(self):
        d = rule_classifier.build_data(self.brand, {"subject": "I want a refund for DD123456",
                                                    "body_text": "return my money"})
        self.assertTrue(d["category"].startswith("7."))
        self.assertTrue(d["requires_agent"])
        self.assertEqual(d["action"], "assign_agent")

    def test_damaged_requests_evidence(self):
        d = rule_classifier.build_data(self.brand, {"subject": "received damaged product",
                                                    "body_text": "the item is broken"})
        self.assertTrue(d["category"].startswith("3."))
        self.assertTrue(d["requires_evidence"])

    def test_damage_typo_and_change_intent_not_uncategorized(self):
        # Reported email (offline fallback misclassified it as Uncategorized): the body
        # says "damage" (not "damaged"), "damage order" and "change it" -- none of which
        # were keywords. It must classify as a damaged-item case with an evidence request
        # and still extract the phone number.
        d = rule_classifier.build_data(self.brand, {
            "subject": "my order is dmage",
            "body_text": "i received damage order so i want to change it\n"
                         "my phone number is 9998070960",
            "from_email": "dabhichintan2134@gmail.com"})
        self.assertTrue(d["is_support_request"])
        self.assertNotEqual(d["category"], "Uncategorized")
        self.assertTrue(d["category"].startswith("3."))         # Damaged -> Delivery Issues
        self.assertTrue(d["requires_evidence"])                 # -> evidence request
        self.assertEqual(d["extracted"]["phone"], "9998070960")

    def test_unknown_is_uncategorized(self):
        d = rule_classifier.build_data(self.brand, {"subject": "hello", "body_text": "just saying hi"})
        self.assertEqual(d["category"], rule_classifier.UNCATEGORIZED)


class FallbackIntegrationTests(BaseFixture):
    def test_classify_uses_rules_without_provider(self):
        # No AI key -> classify() falls back to the rule classifier (default ON).
        result = classifier.classify(self.brand, {"subject": "Where is my order DD9999?",
                                                  "body_text": "tracking please"})
        self.assertIsNotNone(result)
        self.assertEqual(result.category_ref.code, "1")
        self.assertEqual(result.extracted["order_id"], "DD9999")
        self.assertEqual(result.raw.get("engine"), "rules")

    @override_settings(CLASSIFIER_RULE_FALLBACK=False)
    def test_no_fallback_returns_none(self):
        BrandSettings.objects.create(brand=self.brand, ai_api_key="")
        with override_settings(GEMINI_API_KEY=""):
            self.assertIsNone(classifier.classify(self.brand, {"subject": "hi"}))

    def test_ai_error_falls_back_to_rules(self):
        class BoomProvider:
            def generate(self, system, user):
                raise RuntimeError("429 quota exceeded")
        result = classifier.classify(
            self.brand, {"subject": "refund for DD123456", "body_text": "money back"},
            provider=BoomProvider(),
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.category_ref.code, "7")
        self.assertEqual(result.raw.get("engine"), "rules")


class SupportDetectionTests(BaseFixture):
    def test_internal_report_is_not_support(self):
        d = rule_classifier.build_data(self.brand, {
            "subject": "Wukusy Weekly Report - 2026-06-07",
            "body_text": "Attached is the weekly report.", "from_email": "hardip@gmail.com"})
        self.assertFalse(d["is_support_request"])

    def test_noreply_sender_is_not_support(self):
        d = rule_classifier.build_data(self.brand, {
            "subject": "Your order shipped", "body_text": "tracking",
            "from_email": "noreply@vendor.com"})
        self.assertFalse(d["is_support_request"])

    def test_real_customer_is_support(self):
        d = rule_classifier.build_data(self.brand, {
            "subject": "Where is my order DD9999?", "body_text": "tracking please",
            "from_email": "buyer@example.com"})
        self.assertTrue(d["is_support_request"])

    def test_report_fraud_stays_support(self):
        # 'report fraud' must NOT be mistaken for an internal report.
        d = rule_classifier.build_data(self.brand, {
            "subject": "I want to report fraud", "body_text": "scam call asking OTP",
            "from_email": "buyer@example.com"})
        self.assertTrue(d["is_support_request"])

    def test_non_support_ticket_moved_to_ignored(self):
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="hardip@gmail.com", subject="Courier Final Report - 07-06-2026",
        )
        Message.objects.create(ticket=t, direction=Message.DIRECTION_INBOUND,
                               from_email="hardip@gmail.com",
                               subject="Courier Final Report - 07-06-2026", body_text="report")
        classifier.classify_ticket(t)
        t.refresh_from_db()
        self.assertTrue(t.is_ignored)
        self.assertEqual(t.status, Ticket.STATUS_IGNORED)
        self.assertTrue(t.audit_log.filter(event="ignored").exists())


class ClassificationLifecycleTests(BaseFixture):
    def setUp(self):
        super().setUp()
        from apps.organizations.models import Mailbox
        self.mailbox = Mailbox.objects.filter(brand=self.brand).first() or \
            Mailbox.objects.create(brand=self.brand, email_address="care@x.com")

    def _ticket(self, subject="Where is my order DD9999?", body="tracking please"):
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject=subject,
        )
        Message.objects.create(ticket=t, direction=Message.DIRECTION_INBOUND,
                               from_email="b@x.com", subject=subject, body_text=body)
        return t

    def test_gemini_success_sets_ai_classified(self):
        class OK:
            def generate(self, s, u):
                return '{"is_support_request": true, "category": "1. Shipment & Delivery Tracking", "sub_topic": "", "confidence": 0.9}'
        t = self._ticket()
        classifier.classify_ticket(t, provider=OK())
        t.refresh_from_db()
        self.assertEqual(t.classification_status, Ticket.CLS_CLASSIFIED)
        self.assertEqual(t.ai_error, "")

    def test_429_retries_then_fails_and_falls_back(self):
        calls = {"n": 0}
        class Boom:
            def generate(self, s, u):
                calls["n"] += 1
                raise RuntimeError("429 quota exceeded")
        t = self._ticket()
        classifier.classify_ticket(t, provider=Boom())
        t.refresh_from_db()
        self.assertGreater(calls["n"], 1)  # retried
        self.assertEqual(t.classification_status, Ticket.CLS_FAILED)
        self.assertIn("429", t.ai_error)
        self.assertEqual(t.ai_attempts, 1)
        self.assertTrue(t.audit_log.filter(event="ai_error").exists())
        # fallback rules still classified it (for visibility)
        self.assertTrue(t.category.startswith("1."))

    def test_non_retryable_error_does_not_retry(self):
        calls = {"n": 0}
        class Bad:
            def generate(self, s, u):
                calls["n"] += 1
                raise ValueError("bad request")
        t = self._ticket()
        classifier.classify_ticket(t, provider=Bad())
        self.assertEqual(calls["n"], 1)  # not retried (not a 429)

    def test_ai_failed_ticket_does_not_auto_reply(self):
        from apps.decision import engine
        from apps.taxonomy.models import Category, Rule, SubTopic
        # An auto-reply category with high confidence WOULD auto-send -- but because
        # it's AI_FAILED (not Gemini-classified), spec rule 7 forces a draft.
        cat = Category.objects.create(brand=self.brand, code="12", name="Coverage")
        sub = SubTopic.objects.create(category=cat, code="12.1", name="Pincode")
        Rule.objects.create(sub_topic=sub, condition="Always",
                            then_response="We deliver in 3 days.", action=Rule.ACTION_INFO_ONLY)
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="do you deliver?", category="12. Coverage",
            sub_topic_ref=sub, category_ref=cat, status=Ticket.STATUS_CLASSIFIED,
            classification_status=Ticket.CLS_FAILED, ai_confidence=0.95,
        )
        Message.objects.create(ticket=t, direction=Message.DIRECTION_INBOUND,
                               from_email="b@x.com", subject="do you deliver?", body_text="?")
        plan = engine.run(t)
        self.assertNotEqual(plan.send_mode, engine.AUTO)
        self.assertIn("ai_not_classified", plan.reasons)


class ProviderSelectionTests(TestCase):
    def setUp(self):
        from apps.organizations.models import Organization, Brand
        self.org = Organization.objects.create(name="O")
        self.brand = Brand.objects.create(organization=self.org, name="B")

    def test_global_groq_key_selects_groq(self):
        from apps.classifier.providers import get_provider, GroqProvider
        with override_settings(GROQ_API_KEY="gk", GEMINI_API_KEY=""):
            p = get_provider(None)
        self.assertIsInstance(p, GroqProvider)
        self.assertEqual(p.model, "llama-3.3-70b-versatile")

    def test_groq_preferred_over_gemini(self):
        from apps.classifier.providers import get_provider, GroqProvider
        with override_settings(GROQ_API_KEY="gk", GEMINI_API_KEY="ge"):
            self.assertIsInstance(get_provider(None), GroqProvider)

    def test_brand_groq_provider(self):
        from apps.classifier.providers import get_provider, GroqProvider
        s = BrandSettings.objects.create(
            brand=self.brand, ai_provider=BrandSettings.PROVIDER_GROQ, ai_api_key="bk")
        self.assertIsInstance(get_provider(s), GroqProvider)


class OrderIdExtractionTests(TestCase):
    """Regression: a stated order number must be captured even when it's short
    (the reported "my order number is 12345" -> re-asked-for-order-id bug)."""

    def test_short_order_number_with_context(self):
        from apps.classifier.rule_classifier import _extract_order_id
        self.assertEqual(_extract_order_id("my order number is 12345"), "12345")
        self.assertEqual(_extract_order_id("order id: DD9999"), "DD9999")
        self.assertEqual(_extract_order_id("order #4564530"), "4564530")
        self.assertEqual(_extract_order_id("My order id is 123456"), "123456")

    def test_bare_long_number_still_works(self):
        from apps.classifier.rule_classifier import _extract_order_id
        self.assertEqual(_extract_order_id("DD9999 broke"), "DD9999")
        self.assertEqual(_extract_order_id("ref 262203508 please"), "262203508")

    def test_non_order_text_returns_none(self):
        from apps.classifier.rule_classifier import _extract_order_id
        self.assertIsNone(_extract_order_id("where is my order?"))
        self.assertIsNone(_extract_order_id("here is the photo"))


class PhoneNotOrderIdTests(TestCase):
    """A bare mobile number must NOT be extracted as an Order ID (reported bug:
    'my mobile number is 8765321519' -> 'register the complaint for order 8765321519')."""

    def test_bare_mobile_is_not_an_order_id(self):
        from apps.classifier.rule_classifier import _extract_order_id, _extract_phone
        for t in ["my mobile number is 8765321519", "my phone number is 8765321519",
                  "8765321519", "call me 9876543210"]:
            self.assertIsNone(_extract_order_id(t), t)
        self.assertEqual(_extract_phone("my mobile number is 8765321519"), "8765321519")

    def test_explicit_order_keyword_still_wins(self):
        from apps.classifier.rule_classifier import _extract_order_id
        self.assertEqual(_extract_order_id("my order id is 9027510"), "9027510")
        self.assertEqual(_extract_order_id("order 262203508"), "262203508")
