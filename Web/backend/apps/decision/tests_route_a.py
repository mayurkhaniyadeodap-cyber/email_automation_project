"""
Tests for Route A end-to-end: auto-answer wrapped in M4, ticket auto_resolved, and
NO Care Panel ticket / M5 confirmation (golden rule: a ticket exists only when a
human must act).

    python manage.py test apps.decision.tests_route_a
"""

from django.test import TestCase

from apps.brand_settings.models import BrandSettings
from apps.decision import engine
from apps.ingestion import service
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, Rule, SubTopic
from apps.tickets.models import Message, Ticket


class RouteAMailTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.cat = Category.objects.create(brand=self.brand, code="12", name="Serviceability")
        self.sub = SubTopic.objects.create(category=self.cat, code="12.1", name="Coverage")
        Rule.objects.create(sub_topic=self.sub, action=Rule.ACTION_INFO_ONLY, position=1,
                            condition="Always",
                            then_response="Yes, we deliver to your area within 5-7 days.")

    def _ticket(self, lang="en"):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="do you deliver to 560001?",
            category_ref=self.cat, sub_topic_ref=self.sub, language=lang,
            ai_confidence=0.95, classification_status=Ticket.CLS_CLASSIFIED)

    def test_route_a_reply_wrapped_in_m4_and_resolved(self):
        t = self._ticket()
        plan = engine.run(t)
        t.refresh_from_db()
        self.assertEqual(t.extracted.get("route"), "A")
        self.assertEqual(t.status, Ticket.STATUS_AUTO_RESOLVED)
        out = t.messages.get(direction=Message.DIRECTION_OUTBOUND)
        self.assertFalse(out.is_draft)
        self.assertIn("deliver to your area", out.body_text)         # the answer
        self.assertIn("closes your request", out.body_text)          # M4 wrapper
        self.assertIn("reply to reopen", out.body_text)

    def test_route_a_m4_localized(self):
        t = self._ticket(lang="hi")
        engine.run(t)
        out = t.messages.get(direction=Message.DIRECTION_OUTBOUND)
        self.assertIn("रिप्लाई", out.body_text)                       # Hindi M4 wrapper

    def test_draft_route_is_not_wrapped_in_m4(self):
        # A non-classified ticket downgrades AUTO->DRAFT; draft must stay the raw answer.
        t = self._ticket()
        t.classification_status = Ticket.CLS_FAILED
        t.save(update_fields=["classification_status"])
        engine.run(t)
        out = t.messages.get(direction=Message.DIRECTION_OUTBOUND)
        self.assertTrue(out.is_draft)
        self.assertNotIn("closes your request", out.body_text)


class RouteANoCarePanelTests(TestCase):
    """Through the ingestion finalize: Route A creates NO Care Panel ticket / M5."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        self.cat = Category.objects.create(brand=self.brand, code="12", name="Serviceability")
        self.sub = SubTopic.objects.create(category=self.cat, code="12.1", name="Coverage")
        Rule.objects.create(sub_topic=self.sub, action=Rule.ACTION_INFO_ONLY, position=1,
                            condition="Always", then_response="Yes, we deliver there.")

    def test_finalize_skips_care_panel_for_route_a(self):
        from apps.classifier.service import ClassificationResult
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="do you deliver?",
            classification_status=Ticket.CLS_CLASSIFIED)
        result = ClassificationResult(
            category="12. Serviceability", sub_topic="12.1 Coverage", confidence=0.95,
            extracted={}, sentiment="neutral", language="en", is_support_request=True,
            issue_summary="coverage", requires_evidence=False, requires_agent=False,
            category_ref=self.cat, sub_topic_ref=self.sub)

        calls = []
        orig_store = service._store_care_panel
        service._store_care_panel = lambda tk: calls.append(tk.ticket_id)
        try:
            service._finalize_new_ticket(t, result)
        finally:
            service._store_care_panel = orig_store

        t.refresh_from_db()
        self.assertEqual(t.status, Ticket.STATUS_AUTO_RESOLVED)
        self.assertEqual(calls, [])                                  # Care Panel NOT called
        # No M5 "ticket created" confirmation, but the M4 auto-answer did go out.
        self.assertFalse(t.messages.filter(subject="Support Ticket Created Successfully").exists())
        self.assertTrue(t.messages.filter(direction=Message.DIRECTION_OUTBOUND).exists())
