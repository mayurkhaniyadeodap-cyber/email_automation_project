"""
Tests for the per-category business rules (policy.py) and their enforcement in the
decision engine: money / account / fraud categories must never be auto-replied.

    python manage.py test apps.decision.tests_policy
"""

from django.test import TestCase

from apps.brand_settings.models import BrandSettings
from apps.decision import engine, policy
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, Rule, SubTopic
from apps.tickets.models import Message, Ticket


class PolicyMapTests(TestCase):
    def test_category_policies(self):
        self.assertEqual(policy.policy_for("1"), "auto_reply")
        self.assertEqual(policy.policy_for("12"), "auto_reply")
        self.assertEqual(policy.policy_for("6"), "draft_agent")
        self.assertEqual(policy.policy_for("7"), "draft_agent")
        self.assertEqual(policy.policy_for("8"), "agent")
        self.assertEqual(policy.policy_for("14"), "agent")
        self.assertEqual(policy.policy_for("16"), "escalate")
        self.assertIsNone(policy.policy_for("3"))

    def test_allows_auto_reply(self):
        self.assertTrue(policy.allows_auto_reply("1"))
        self.assertTrue(policy.allows_auto_reply("3"))  # no policy -> allowed
        self.assertFalse(policy.allows_auto_reply("8"))
        self.assertFalse(policy.allows_auto_reply("6"))
        self.assertFalse(policy.allows_auto_reply("16"))


class ConstraintTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, confidence_threshold=0.75)

    def _sub(self, cat_code, cat_name, code, name, action):
        cat, _ = Category.objects.get_or_create(
            brand=self.brand, code=cat_code, defaults={"name": cat_name}
        )
        sub = SubTopic.objects.create(category=cat, code=code, name=name)
        Rule.objects.create(
            sub_topic=sub, condition="Always", then_response="Here you go.", action=action
        )
        return sub

    def _ticket(self, sub):
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="hi", sub_topic_ref=sub,
            category_ref=sub.category, status=Ticket.STATUS_CLASSIFIED,
            classification_status=Ticket.CLS_CLASSIFIED, ai_confidence=0.95,
        )
        Message.objects.create(ticket=t, direction=Message.DIRECTION_INBOUND,
                               from_email="b@x.com", subject="hi", body_text="?")
        return t

    def test_auto_reply_category_still_auto_sends(self):
        # Category 12 (Delivery Coverage) is AUTO_REPLY -> info_only auto-sends.
        sub = self._sub("12", "Delivery Coverage", "12.1", "Pincode", Rule.ACTION_INFO_ONLY)
        plan = engine.decide(self._ticket(sub))
        self.assertEqual(plan.send_mode, engine.AUTO)
        self.assertEqual(plan.status, Ticket.STATUS_AUTO_RESOLVED)

    def test_payment_category_never_auto_sends(self):
        # Category 8 (Payment) is AGENT -> even an info_only rule must NOT auto-send.
        sub = self._sub("8", "Payment & Invoice", "8.1", "Refund status", Rule.ACTION_INFO_ONLY)
        plan = engine.decide(self._ticket(sub))
        self.assertNotEqual(plan.send_mode, engine.AUTO)
        self.assertEqual(plan.status, Ticket.STATUS_AWAITING_AGENT)
        self.assertIn("category_agent", plan.reasons)

    def test_cancellation_category_drafts_for_agent(self):
        # Category 6 (Order Cancellation) is DRAFT_AGENT.
        sub = self._sub("6", "Order Cancellation", "6.9", "Cancel", Rule.ACTION_INFO_ONLY)
        plan = engine.decide(self._ticket(sub))
        self.assertEqual(plan.send_mode, engine.DRAFT)
        self.assertEqual(plan.status, Ticket.STATUS_AWAITING_AGENT)
        self.assertIn("category_draft_agent", plan.reasons)

    def test_fraud_category_escalates(self):
        # Category 16 (Fraud) is ESCALATE (non-sensitive sub-topic still escalates).
        sub = self._sub("16", "Feedback, Support & Fraud", "16.9", "Issue", Rule.ACTION_INFO_ONLY)
        plan = engine.decide(self._ticket(sub))
        self.assertNotEqual(plan.send_mode, engine.AUTO)
        self.assertEqual(plan.status, Ticket.STATUS_ESCALATED)
        self.assertEqual(plan.priority, Ticket.PRIORITY_HIGH)
        self.assertTrue(plan.create_agent_task)


class ResponderFallbackTests(TestCase):
    """When a sendable plan has no template text, the AI responder fills it in."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k")
        cat = Category.objects.create(brand=self.brand, code="9", name="Product Information")
        self.sub = SubTopic.objects.create(category=cat, code="9.1", name="Specs")
        # info_only rule with NO then_response and no template -> empty reply text.
        Rule.objects.create(sub_topic=self.sub, condition="Always", then_response="",
                            action=Rule.ACTION_INFO_ONLY)

    def test_generated_reply_used_when_no_template(self):
        from apps.classifier import service as classifier

        class FakeProvider:
            def generate_text(self, system, user):
                return "Thanks for reaching out! Here are the product details you asked for."

        original = classifier.build_provider
        classifier.build_provider = lambda settings: FakeProvider()
        try:
            t = Ticket.objects.create(
                organization=self.org, brand=self.brand, mailbox=self.mailbox,
                customer_email="b@x.com", subject="specs?", sub_topic_ref=self.sub,
                category_ref=self.sub.category, status=Ticket.STATUS_CLASSIFIED,
                classification_status=Ticket.CLS_CLASSIFIED, ai_confidence=0.95,
            )
            Message.objects.create(
                ticket=t, direction=Message.DIRECTION_INBOUND, from_email="b@x.com",
                subject="specs?", body_text="what size?",
            )
            engine.run(t)
        finally:
            classifier.build_provider = original

        sent = t.messages.filter(direction=Message.DIRECTION_OUTBOUND).first()
        self.assertIsNotNone(sent)
        self.assertIn("product details", sent.body_text)
