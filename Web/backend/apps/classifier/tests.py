"""
Offline tests for the Phase 3 AI classifier (doc sections 4 & 5).

A FakeProvider stands in for Gemini/ChatGPT, so the whole classify -> map ->
apply -> ingest pipeline is exercised without any API key or network. Run with:

    python manage.py test apps.classifier
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.brand_settings.models import BrandSettings
from apps.classifier import service as classifier
from apps.classifier import skills
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, Rule, SubTopic
from apps.tickets.models import Message, Ticket

User = get_user_model()


class FakeProvider:
    """Returns a canned response (dict -> JSON, or raw string) for any prompt."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        if isinstance(self.response, str):
            return self.response
        return json.dumps(self.response)


DAMAGED = {
    "category": "3. Delivery Issues (Post-Delivery)",
    "sub_topic": "3.3 Shipment Lost or Damaged",
    "confidence": 0.91,
    "extracted": {
        "order_id": "DD123456",
        "awb": None,
        "has_unboxing_video": False,
        "has_photo": True,
        "customer_intent": "report damaged product",
    },
    "language": "en",
    "sentiment": "frustrated",
}


class BaseFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(
            brand=self.brand, email_address="care@deodap.com"
        )
        self.settings = BrandSettings.objects.create(
            brand=self.brand, ai_provider=BrandSettings.PROVIDER_GEMINI,
            ai_api_key="test-key", confidence_threshold=0.75,
        )
        # Minimal taxonomy.
        self.cat3 = Category.objects.create(
            brand=self.brand, code="3", name="Delivery Issues (Post-Delivery)"
        )
        self.sub33 = SubTopic.objects.create(
            category=self.cat3, code="3.3", name="Shipment Lost or Damaged",
            question="Is the item lost or damaged?", mandatory_inputs=["order_id"],
        )
        Rule.objects.create(
            sub_topic=self.sub33, condition="No unboxing video",
            then_response="Please share the unboxing video.",
            action=Rule.ACTION_AWAIT_EVIDENCE,
        )
        self.cat16 = Category.objects.create(
            brand=self.brand, code="16", name="Feedback, Support & Fraud"
        )
        self.sub162 = SubTopic.objects.create(
            category=self.cat16, code="16.2", name="Report Fraud", is_sensitive=True,
        )

    def make_ticket(self, body="Product arrived broken", subject="Broken item"):
        ticket = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            thread_id="t1", customer_email="buyer@example.com", subject=subject,
        )
        Message.objects.create(
            ticket=ticket, direction=Message.DIRECTION_INBOUND,
            from_email="buyer@example.com", subject=subject, body_text=body,
        )
        return ticket


class SkillsTests(BaseFixture):
    def test_knowledge_base_lists_codes_and_rules(self):
        kb = skills.build_knowledge_base(self.brand)
        self.assertIn("3. Delivery Issues (Post-Delivery)", kb)
        self.assertIn("3.3 Shipment Lost or Damaged", kb)
        self.assertIn("16.2 Report Fraud [SENSITIVE", kb)
        self.assertIn("Await evidence", kb)

    def test_prompt_includes_mail_and_taxonomy(self):
        system, user = skills.build_prompt(
            self.brand,
            {"from_email": "b@x.com", "subject": "broken", "body_text": "it broke",
             "attachments": [{"filename": "p.jpg", "mime_type": "image/jpeg"}]},
        )
        self.assertIn("FIXED TAXONOMY", system)
        self.assertIn("3.3 Shipment Lost or Damaged", system)
        self.assertIn("Subject: broken", user)
        self.assertIn("p.jpg", user)


class ClassifyMappingTests(BaseFixture):
    def test_maps_to_taxonomy_by_code(self):
        result = classifier.classify(
            self.brand, {"subject": "x", "body_text": "y"},
            provider=FakeProvider(DAMAGED),
        )
        self.assertEqual(result.sub_topic_ref, self.sub33)
        self.assertEqual(result.category_ref, self.cat3)
        self.assertEqual(result.sub_topic, "3.3 Shipment Lost or Damaged")
        self.assertAlmostEqual(result.confidence, 0.91)
        self.assertEqual(result.extracted["order_id"], "DD123456")
        self.assertFalse(result.is_uncategorized)

    def test_maps_by_subtopic_code_even_with_loose_category(self):
        resp = {**DAMAGED, "category": "garbage", "sub_topic": "3.3 whatever name"}
        result = classifier.classify(
            self.brand, {"body_text": "y"}, provider=FakeProvider(resp)
        )
        self.assertEqual(result.sub_topic_ref, self.sub33)
        # Canonicalized back to the real taxonomy strings.
        self.assertEqual(result.category, "3. Delivery Issues (Post-Delivery)")

    def test_unknown_code_falls_back_to_uncategorized(self):
        resp = {**DAMAGED, "category": "99. Nope", "sub_topic": "99.9 Nope"}
        result = classifier.classify(
            self.brand, {"body_text": "y"}, provider=FakeProvider(resp)
        )
        self.assertTrue(result.is_uncategorized)
        self.assertEqual(result.category, skills.UNCATEGORIZED)
        self.assertEqual(result.sub_topic, "")

    def test_confidence_clamped(self):
        resp = {**DAMAGED, "confidence": 5}
        result = classifier.classify(
            self.brand, {"body_text": "y"}, provider=FakeProvider(resp)
        )
        self.assertEqual(result.confidence, 1.0)

    def test_parses_json_inside_code_fences(self):
        fenced = "```json\n" + json.dumps(DAMAGED) + "\n```"
        result = classifier.classify(
            self.brand, {"body_text": "y"}, provider=FakeProvider(fenced)
        )
        self.assertEqual(result.sub_topic_ref, self.sub33)

    @override_settings(CLASSIFIER_RULE_FALLBACK=False)
    def test_no_provider_returns_none(self):
        self.settings.ai_api_key = ""
        self.settings.save()
        # No brand key, no global env key, and rule fallback OFF -> None.
        with override_settings(GEMINI_API_KEY=""):
            result = classifier.classify(self.brand, {"body_text": "y"})
        self.assertIsNone(result)


class ApplyToTicketTests(BaseFixture):
    def test_classify_ticket_writes_fields_and_audits(self):
        ticket = self.make_ticket()
        result = classifier.classify_ticket(ticket, provider=FakeProvider(DAMAGED))
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_CLASSIFIED)
        self.assertEqual(ticket.sub_topic_ref, self.sub33)
        self.assertEqual(ticket.category, "3. Delivery Issues (Post-Delivery)")
        self.assertAlmostEqual(ticket.ai_confidence, 0.91)
        self.assertEqual(ticket.sentiment, "frustrated")
        self.assertEqual(ticket.extracted["order_id"], "DD123456")
        self.assertEqual(ticket.mandatory_inputs, ["order_id"])
        self.assertTrue(ticket.audit_log.filter(event="classified").exists())
        self.assertEqual(result.sub_topic_ref, self.sub33)

    def test_classify_ticket_skips_ignored(self):
        ticket = self.make_ticket()
        ticket.is_ignored = True
        ticket.save()
        self.assertIsNone(
            classifier.classify_ticket(ticket, provider=FakeProvider(DAMAGED))
        )

    def test_not_support_but_real_category_is_not_ignored(self):
        # Reported bug: a franchise inquiry was AI-flagged is_support_request=false (as
        # "promotional") yet given category 11 -> it was wrongly Ignored with no reply.
        franchise = {"is_support_request": False,
                     "category": "11. Wholesale / Bulk Purchase (B2B)", "sub_topic": "",
                     "confidence": 0.8, "issue_summary": "Franchise inquiry",
                     "requires_evidence": False, "requires_agent": False,
                     "action": "auto_reply", "extracted": {}, "language": "en",
                     "sentiment": "neutral"}
        Category.objects.create(brand=self.brand, code="11",
                                name="Wholesale / Bulk Purchase (B2B)")
        ticket = self.make_ticket(subject="Franchise Inquiry",
                                  body="I am interested in becoming a DeoDap franchise partner")
        classifier.classify_ticket(ticket, provider=FakeProvider(franchise))
        ticket.refresh_from_db()
        self.assertFalse(ticket.is_ignored)                          # NOT ignored
        self.assertEqual(ticket.status, Ticket.STATUS_CLASSIFIED)
        self.assertEqual(ticket.category, "11. Wholesale / Bulk Purchase (B2B)")
        self.assertFalse(ticket.audit_log.filter(event="ignored").exists())

    def test_not_support_uncategorized_is_still_ignored(self):
        newsletter = {"is_support_request": False, "category": skills.UNCATEGORIZED,
                      "sub_topic": "", "confidence": 0.3, "issue_summary": "newsletter",
                      "requires_evidence": False, "requires_agent": False,
                      "action": "ignore", "extracted": {}, "language": "en",
                      "sentiment": "neutral"}
        ticket = self.make_ticket(subject="Weekly Deals", body="Big sale this week!")
        classifier.classify_ticket(ticket, provider=FakeProvider(newsletter))
        ticket.refresh_from_db()
        self.assertTrue(ticket.is_ignored)                           # still ignored
        self.assertTrue(ticket.audit_log.filter(event="ignored").exists())


class IngestionAutoClassifyTests(BaseFixture):
    def test_sync_history_auto_classifies(self):
        from apps.ingestion import service as ingestion
        from apps.ingestion.tests import FakeGmailClient, gmail_raw

        fake_provider = FakeProvider(DAMAGED)
        original = classifier.build_provider
        classifier.build_provider = lambda settings: fake_provider
        try:
            # Damaged is a PHOTO-evidence category -> include a photo so the email
            # proceeds to a ticket (instead of being held for evidence).
            client = FakeGmailClient([gmail_raw(
                text="my product is broken",
                attachments=[{"filename": "photo.jpg", "mime_type": "image/jpeg"}])])
            ingestion.sync_history(self.mailbox, client=client)
        finally:
            classifier.build_provider = original

        ticket = Ticket.objects.get(thread_id="t1")
        # Classification ran (the engine then advances status past 'classified').
        self.assertEqual(ticket.sub_topic_ref, self.sub33)
        self.assertTrue(ticket.audit_log.filter(event="classified").exists())
        self.assertTrue(ticket.audit_log.filter(event="decision").exists())


class ClassifyEndpointTests(BaseFixture):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("agent", password="pw")
        self.org.members.add(self.user)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_classify_action_returns_updated_ticket(self):
        ticket = self.make_ticket()
        fake_provider = FakeProvider(DAMAGED)
        original = classifier.build_provider
        classifier.build_provider = lambda settings: fake_provider
        try:
            resp = self.api.post(f"/api/tickets/{ticket.id}/classify/")
        finally:
            classifier.build_provider = original
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["sub_topic"], "3.3 Shipment Lost or Damaged")

    @override_settings(GEMINI_API_KEY="", CLASSIFIER_RULE_FALLBACK=False)
    def test_classify_action_409_without_provider(self):
        self.settings.ai_api_key = ""
        self.settings.save()
        ticket = self.make_ticket()
        resp = self.api.post(f"/api/tickets/{ticket.id}/classify/")
        self.assertEqual(resp.status_code, 409)


class DeliveredItemSubtypeCorrectionTests(BaseFixture):
    """The AI's Delivered-Item sub-type is deterministically corrected from keywords so a
    damage email can never be classified 'Missing Item' (the reported bug)."""

    def setUp(self):
        super().setUp()
        self.sub_damaged = SubTopic.objects.create(
            category=self.cat3, code="3.10", name="Damaged Item")
        self.sub_missing = SubTopic.objects.create(
            category=self.cat3, code="3.11", name="Missing Item")

    def _classify(self, subject, body, ai_sub_topic):
        ai = {**DAMAGED, "sub_topic": ai_sub_topic}
        return classifier.classify(
            self.brand,
            {"subject": subject, "body_text": body, "from_email": "buyer@example.com"},
            provider=FakeProvider(ai))

    def test_damage_email_misclassified_missing_is_corrected(self):
        # AI says "Missing Item"; text is a damage complaint -> corrected to "Damaged Item".
        r = self._classify("My order is damage", "My order is damage. I want to return it.",
                           "3.11 Missing Item")
        self.assertIn("Damaged Item", r.sub_topic)
        self.assertNotIn("Missing Item", r.sub_topic)
        self.assertEqual(r.sub_topic_ref, self.sub_damaged)

    def test_genuine_missing_item_unchanged(self):
        r = self._classify("Item missing", "one item is missing from my order",
                           "3.11 Missing Item")
        self.assertIn("Missing Item", r.sub_topic)

    def test_wrong_item_corrected(self):
        SubTopic.objects.create(category=self.cat3, code="3.12", name="Wrong Item")
        r = self._classify("wrong", "I received wrong product", "3.11 Missing Item")
        self.assertIn("Wrong Item", r.sub_topic)


class PhoneExtractionTests(TestCase):
    """Every way a customer writes their mobile must extract the bare 10-digit number
    (the reported failure: 'mobile : 7004810519' replied 'could not verify')."""

    PHONE = "7004810519"

    def test_all_mobile_formats_extract(self):
        from apps.classifier.rule_classifier import _extract_phone, _extract_order_id
        cases = [
            "mobile : 7004810519", "mobile:7004810519", "mobile number:7004810519",
            "my mobile is 7004810519", "phone 7004810519", "7004810519",
            "+91 7004810519", "+917004810519", "0 7004810519",
            "contact me on 91-7004810519",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(_extract_phone(text), self.PHONE)
                self.assertIsNone(_extract_order_id(text))   # a mobile is never an order id

    def test_normalize_phone_strips_country_code(self):
        from apps.classifier.rule_classifier import normalize_phone
        for raw in ("7004810519", "+917004810519", "917004810519", "07004810519",
                    "+91 70048 10519", "91 7004810519"):
            self.assertEqual(normalize_phone(raw), self.PHONE)
        self.assertEqual(normalize_phone("12345"), "")          # not a mobile
        self.assertEqual(normalize_phone("1234567890"), "")     # starts < 6


class WebsiteAppOverrideTests(BaseFixture):
    """Website / mobile-app fault keywords MUST force Category 15 (Website / App Related) and
    the right sub-topic -- never a Delivery / Tracking / Item issue (the reported bug)."""

    def setUp(self):
        super().setUp()
        self.cat15 = Category.objects.create(
            brand=self.brand, code="15", name="App & Website Technical Issues")

    def _classify(self, subject, body, ai=None):
        # Default: the AI MISCLASSIFIES as a delivery "Other Issue" -> override must win.
        ai = ai or {"category": "3. Delivery Issues (Post-Delivery)",
                    "sub_topic": "3.9 Other Issue", "confidence": 0.9}
        return classifier.classify(
            self.brand,
            {"subject": subject, "body_text": body, "from_email": "buyer@example.com"},
            provider=FakeProvider(ai))

    def test_app_crash_forces_website_app_category(self):
        r = self._classify("App crash", "App crashes after installation")
        self.assertEqual(r.category_ref, self.cat15)
        self.assertIn("15.", r.category)
        self.assertEqual(r.sub_topic, "App Crashing / Not Loading")
        self.assertFalse(r.requires_evidence)

    def test_subtopic_resolution(self):
        cases = {
            "the app keeps crashing on launch": "App Crashing / Not Loading",
            "app not loading at all": "App Crashing / Not Loading",
            "website not opening on my phone": "App Crashing / Not Loading",
            "checkout page not loading": "Checkout Page Not Load",
            "my cart not saving items": "Cart Not Saving Items",
            "saved address missing from my account": "Saved Address Not Found",
            "browser compatibility issue on edge": "Browser & Device Support",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                r = self._classify("issue", text)
                self.assertEqual(r.category_ref, self.cat15, text)
                self.assertEqual(r.sub_topic, expected, text)

    def test_never_classified_as_delivery_or_item(self):
        for text in ("app crashing", "website not opening", "checkout page not load",
                     "cart not saving", "saved address missing", "browser issue"):
            with self.subTest(text=text):
                # Even if the AI insists it's a missing/wrong item, the override wins.
                r = self._classify("x", text,
                                   ai={"category": "3. Delivery Issues", "sub_topic": "Missing Item",
                                       "confidence": 0.95})
                self.assertEqual(r.category_ref.code, "15")
                self.assertNotIn("Missing Item", r.sub_topic)

    def test_genuine_delivery_email_untouched(self):
        # No app/website keyword -> the normal pipeline (delivery) is preserved.
        r = self._classify("where is my order", "track my order please",
                           ai={"category": "1. Shipment & Delivery Tracking",
                               "sub_topic": "Shipment Tracking", "confidence": 0.9})
        self.assertNotEqual((r.category_ref and r.category_ref.code), "15")

    def test_override_logs_emitted(self):
        with self.assertLogs("apps.classifier.service", level="INFO") as cm:
            self._classify("App crash", "App crashes after installation")
        blob = "\n".join(cm.output)
        self.assertIn("CLASSIFICATION-TOPIC=", blob)
        self.assertIn("CLASSIFICATION-SUBTOPIC=", blob)
        self.assertIn("CLASSIFICATION-REASON=", blob)

    def test_exact_trace_logs_for_app_crashing_after_login(self):
        # The reported input -> CLASSIFICATION_TOPIC/SUBTOPIC trace (underscore form requested).
        with self.assertLogs("apps.classifier.service", level="INFO") as cm:
            self._classify("App crashing", "App crashing after login")
        blob = "\n".join(cm.output)
        self.assertIn("CLASSIFICATION_TOPIC=Website / App Related", blob)
        self.assertIn("CLASSIFICATION_SUBTOPIC=App Crashing / Not Loading", blob)


class AppCrashingForceClassificationTests(BaseFixture):
    """The exact reported bug: 'App Crashing' emails must ALWAYS classify as
    Website / App Related -> App Crashing / Not Loading, never a Delivery category."""

    def setUp(self):
        super().setUp()
        self.cat15 = Category.objects.create(
            brand=self.brand, code="15", name="App & Website Technical Issues")

    def _classify(self, subject, body):
        # AI MISCLASSIFIES as 'Other Delivery Related Issue' -> override must force cat 15.
        ai = {"category": "3. Delivery Issues", "sub_topic": "Other Delivery Related Issue",
              "confidence": 0.9}
        return classifier.classify(
            self.brand, {"subject": subject, "body_text": body, "from_email": "b@x.com"},
            provider=FakeProvider(ai))

    def test_reported_bug_input(self):
        r = self._classify("App Crashing", "Hi i install app but App Crashing")
        self.assertEqual(r.category_ref, self.cat15)
        self.assertEqual(r.sub_topic, "App Crashing / Not Loading")

    def test_all_app_crash_keywords_force_website_app(self):
        for body in ("app crashing", "app crash", "application crash",
                     "app not loading", "app not opening"):
            with self.subTest(body=body):
                r = self._classify("issue", body)
                self.assertEqual(r.category_ref.code, "15", body)
                self.assertEqual(r.sub_topic, "App Crashing / Not Loading", body)
                self.assertNotIn("Delivery", r.category, body)


class BlankSubjectClassificationTests(BaseFixture):
    """A BLANK subject must never push a ticket into a wrong fallback (Other Delivery / Multiple
    Issues): classification reads the BODY + identifiers, so the deterministic overrides still
    fire with subject=''."""

    def setUp(self):
        super().setUp()
        self.cat8 = Category.objects.create(brand=self.brand, code="8", name="Payment & Invoice")
        self.cat15 = Category.objects.create(
            brand=self.brand, code="15", name="App & Website Technical Issues")

    def _classify_blank(self, body):
        # SUBJECT BLANK + AI mislabels as delivery -> override must still fix it from the body.
        ai = {"category": "3. Delivery Issues", "sub_topic": "Other Delivery Related Issue",
              "confidence": 0.9}
        return classifier.classify(
            self.brand, {"subject": "", "body_text": body, "from_email": "b@x.com"},
            provider=FakeProvider(ai))

    def test_blank_subject_payment_no_order(self):
        r = self._classify_blank("Customer claims to have made a payment but the order was not placed")
        self.assertEqual(r.category_ref.code, "8")
        self.assertEqual(r.sub_topic, "Payment Deducted But Order Not Placed")
        self.assertNotIn("Delivery", r.category)

    def test_blank_subject_app_crashing(self):
        r = self._classify_blank("App crashing after login")
        self.assertEqual(r.category_ref.code, "15")
        self.assertEqual(r.sub_topic, "App Crashing / Not Loading")
        self.assertNotIn("Delivery", r.category)


class OffersClassificationTests(BaseFixture):
    """Offer / discount / coupon inquiries must classify as Ongoing Offers & Sales (cat 10),
    never a Delivery / Missing-Item issue (the reported bug)."""

    def setUp(self):
        super().setUp()
        self.cat10 = Category.objects.create(
            brand=self.brand, code="10", name="Offers, Discounts & Loyalty")

    def _classify(self, body):
        # AI MISCLASSIFIES as a delivery issue -> the override must force cat 10.
        ai = {"category": "3. Delivery Issues", "sub_topic": "Other Delivery Related Issue",
              "confidence": 0.9}
        return classifier.classify(
            self.brand, {"subject": body, "body_text": body, "from_email": "b@x.com"},
            provider=FakeProvider(ai))

    def test_general_offer_inquiry(self):
        r = self._classify("Any current offers?")
        self.assertEqual(r.category_ref, self.cat10)
        self.assertEqual(r.sub_topic, "Offer Inquiry")
        self.assertNotIn("Delivery", r.category)

    def test_discount_problem(self):
        for body in ("Promo code not working", "Coupon not applying", "Sale price not showing"):
            with self.subTest(body=body):
                r = self._classify(body)
                self.assertEqual(r.category_ref.code, "10")
                self.assertEqual(r.sub_topic, "Discount Issue")

    def test_all_offer_keywords_force_cat10(self):
        for body in ("any offer", "discount available", "coupon", "promo code", "sale price",
                     "great deal", "loyalty points"):
            with self.subTest(body=body):
                r = self._classify(body)
                self.assertEqual(r.category_ref.code, "10", body)
                self.assertNotIn("Delivery", r.category, body)

    def test_logs_emitted(self):
        with self.assertLogs("apps.classifier.service", level="INFO") as cm:
            self._classify("Any current offers?")
        blob = "\n".join(cm.output)
        self.assertIn("CLASSIFICATION-TOPIC=Ongoing Offers & Sales", blob)
        self.assertIn("CLASSIFICATION-SUBTOPIC=", blob)
        self.assertIn("CLASSIFICATION-REASON=", blob)

    def test_wholesale_not_offers(self):
        # 'wholesale' contains 'sale' but must NOT trigger the offers override.
        r = self._classify("I want wholesale bulk pricing")
        self.assertNotEqual((r.category_ref and r.category_ref.code), "10")


class PaymentNoOrderClassificationTests(BaseFixture):
    """'Payment deducted but order not placed' emails must ALWAYS classify as Payment & Invoice
    (cat 8) -> 'Payment Deducted But Order Not Placed', NEVER a Delivery / item category."""

    def setUp(self):
        super().setUp()
        self.cat8 = Category.objects.create(brand=self.brand, code="8", name="Payment & Invoice")

    def _classify(self, body, subject=""):
        # AI MISCLASSIFIES as a delivery issue -> the override must force cat 8.
        ai = {"category": "3. Delivery Issues", "sub_topic": "Other Delivery Related Issue",
              "confidence": 0.9}
        return classifier.classify(
            self.brand, {"subject": subject, "body_text": body, "from_email": "b@x.com"},
            provider=FakeProvider(ai))

    PHRASES = (
        "Customer Claims to have made a payment but the order was not placed",
        "payment deducted but order not placed",
        "money deducted but order not received",
        "amount debited but no order",
        "payment successful but order not created",
        "payment completed but order missing",
        "order not placed after payment",
        "transaction successful but no order confirmation",
        "Payment was payed but order for not palced",   # the real typo'd phrasing
        "Payment of 499 deducted but no order confirmation received",
    )

    def test_reported_test_case(self):
        r = self._classify("Customer Claims to have made a payment but the order was not placed. "
                           "mobile number : 8078518087")
        self.assertEqual(r.category_ref, self.cat8)
        self.assertEqual(r.sub_topic, "Payment Deducted But Order Not Placed")
        self.assertTrue(r.requires_evidence)
        self.assertNotIn("Delivery", r.category)

    def test_every_phrase_maps_to_payment_never_delivery(self):
        BANNED = ("Delivery", "Tracking", "Missing", "Damaged", "Wrong", "Quality")
        for body in self.PHRASES:
            with self.subTest(body=body):
                r = self._classify(body)
                self.assertEqual(r.category_ref.code, "8", body)
                self.assertEqual(r.sub_topic, "Payment Deducted But Order Not Placed", body)
                for bad in BANNED:
                    self.assertNotIn(bad, r.category, f"{body!r} -> {r.category!r}")

    def test_logs_emitted(self):
        with self.assertLogs("apps.classifier.service", level="INFO") as cm:
            self._classify("payment deducted but order not placed")
        blob = "\n".join(cm.output)
        self.assertIn("CLASSIFICATION_TOPIC=Payment Issue", blob)
        self.assertIn("CLASSIFICATION_SUBTOPIC=Payment Deducted But Order Not Placed", blob)

    def test_genuine_delivery_not_misrouted_to_payment(self):
        r = self._classify("where is my order, not received yet")
        self.assertNotEqual((r.category_ref and r.category_ref.code), "8")
