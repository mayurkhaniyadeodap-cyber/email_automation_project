"""
Offline tests for the Phase 4 decision engine (doc sections 5, 6, 7).

Builds a small deterministic taxonomy and walks the §7 worked-example matrix plus
every §6 guardrail. No AI / network -- the classifier is bypassed by setting the
ticket's classification fields directly, and the engine is pure logic.

    python manage.py test apps.decision
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.brand_settings.models import BrandSettings
from apps.decision import engine
from apps.decision.engine import evaluate_condition
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, Rule, SubTopic, Template
from apps.tickets.models import Message, Ticket

User = get_user_model()


class BaseFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(
            brand=self.brand, email_address="care@deodap.com"
        )
        self.settings = BrandSettings.objects.create(
            brand=self.brand, confidence_threshold=0.75, await_evidence_autosend=True,
        )
        self._build_taxonomy()

    def _sub(self, cat_code, cat_name, code, name, **kw):
        cat, _ = Category.objects.get_or_create(
            brand=self.brand, code=cat_code, defaults={"name": cat_name}
        )
        return SubTopic.objects.create(category=cat, code=code, name=name, **kw)

    def _build_taxonomy(self):
        # 1.1 info_only but condition needs live data (unevaluable in Phase 4).
        self.s11 = self._sub("1", "Shipment & Delivery Tracking", "1.1",
                             "Shipment Status", mandatory_inputs=["order_id"])
        Rule.objects.create(sub_topic=self.s11, condition="Order shipped AND EDD not breached",
                            then_response="Track: {tracking_url}", action=Rule.ACTION_INFO_ONLY)

        # 1.2 info_only, evaluable ("Always") but template has a live placeholder.
        self.s12 = self._sub("1", "Shipment & Delivery Tracking", "1.2", "Where is it")
        Rule.objects.create(sub_topic=self.s12, condition="Always",
                            then_response="Track here: {tracking_url}",
                            action=Rule.ACTION_INFO_ONLY)

        # 12.1 info_only, "Always", clean template -> safe auto-send.
        self.s121 = self._sub("12", "Delivery Coverage & Shipping", "12.1",
                              "Pincode / Serviceability")
        Rule.objects.create(sub_topic=self.s121, condition="Always",
                            then_response="We deliver to your area in 3 days.",
                            action=Rule.ACTION_INFO_ONLY)
        Template.objects.create(sub_topic=self.s121, name="default",
                                body="Yes! We deliver to your area in 3 days.")

        # 3.3 await_evidence then create_ticket (evidence-based conditions).
        self.s33 = self._sub("3", "Delivery Issues (Post-Delivery)", "3.3",
                             "Shipment Lost or Damaged", mandatory_inputs=["order_id"])
        Rule.objects.create(sub_topic=self.s33, position=1,
                            condition="No unboxing video / photo evidence present",
                            then_response="Please share an unboxing video or photo.",
                            action=Rule.ACTION_AWAIT_EVIDENCE)
        Rule.objects.create(sub_topic=self.s33, position=2, condition="Evidence present",
                            then_response="Complaint registered; routed to agent.",
                            action=Rule.ACTION_CREATE_TICKET)

        # 6.1 trigger cancellation/refund (evaluable via "Any ...").
        self.s61 = self._sub("6", "Order Cancellation", "6.1", "Cancel / Refund")
        Rule.objects.create(sub_topic=self.s61, condition="Any cancellation request",
                            then_response="Cancellation + refund triggered.",
                            action=Rule.ACTION_TRIGGER_CRP)

        # 16.2 sensitive (always human).
        self.s162 = self._sub("16", "Feedback, Support & Fraud", "16.2",
                             "Report Fraud", is_sensitive=True)
        Rule.objects.create(sub_topic=self.s162, condition="Any fraud report",
                            then_response="Human only.", action=Rule.ACTION_CREATE_TICKET)

    def make_ticket(self, sub, *, extracted=None, confidence=0.9, sentiment="neutral",
                    body="hello"):
        ticket = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            thread_id=f"t-{sub.code if sub else 'none'}", customer_email="buyer@example.com",
            subject="help", sub_topic_ref=sub,
            category_ref=sub.category if sub else None,
            status=Ticket.STATUS_CLASSIFIED, classification_status=Ticket.CLS_CLASSIFIED,
            ai_confidence=confidence,
            sentiment=sentiment, extracted=extracted or {},
            mandatory_inputs=sub.mandatory_inputs if sub else [],
        )
        Message.objects.create(
            ticket=ticket, direction=Message.DIRECTION_INBOUND,
            from_email="buyer@example.com", subject="help", body_text=body,
        )
        return ticket


class ConditionEvalTests(BaseFixture):
    def test_always_and_any(self):
        self.assertIs(evaluate_condition("Always", {}), True)
        self.assertIs(evaluate_condition("Any fraud report", {}), True)
        self.assertIs(evaluate_condition("", {}), True)

    def test_evidence_conditions(self):
        self.assertIs(evaluate_condition("Evidence present", {"has_photo": True}), True)
        self.assertIs(evaluate_condition("Evidence present", {}), False)
        self.assertIs(
            evaluate_condition("No unboxing video / photo evidence present", {}), True
        )
        self.assertIs(
            evaluate_condition("No unboxing video / photo evidence present",
                               {"has_unboxing_video": True}), False
        )

    def test_live_data_is_unevaluable(self):
        self.assertIsNone(evaluate_condition("Order shipped AND EDD not breached", {}))
        self.assertIsNone(evaluate_condition("Not dispatched AND not a custom item", {}))


class InfoOnlyTests(BaseFixture):
    def test_clean_info_only_auto_sends_and_resolves(self):
        ticket = self.make_ticket(self.s121)
        plan = engine.run(ticket)
        self.assertEqual(plan.action_code, Rule.ACTION_INFO_ONLY)
        self.assertEqual(plan.send_mode, engine.AUTO)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_AUTO_RESOLVED)
        self.assertEqual(ticket.priority, Ticket.PRIORITY_LOW)
        self.assertTrue(ticket.ai_handled)
        self.assertIsNone(ticket.sla_due_at)  # auto-resolved: no SLA clock
        sent = ticket.messages.get(direction=Message.DIRECTION_OUTBOUND)
        self.assertFalse(sent.is_draft)
        self.assertIsNotNone(sent.sent_at)
        self.assertIn("deliver to your area", sent.body_text)

    def test_unresolved_placeholder_not_sent_and_no_ticket_draft(self):
        # Cat 1 (Shipment Tracking) is a NO_TICKET info category. A half-filled template
        # ({tracking_url} the lookup couldn't fill) must NEVER be sent verbatim. With no
        # AI responder available it falls back to an agent -- but it does not draft the
        # raw placeholder (the old behavior that turned tracking into a ticket).
        ticket = self.make_ticket(self.s12)
        plan = engine.run(ticket)
        self.assertNotIn("unresolved_placeholders", plan.reasons)
        self.assertIn("policy_auto_reply", plan.reasons)        # entered the auto-reply flow
        self.assertIn("no_auto_answer", plan.reasons)           # but no answer w/o AI -> agent
        out = ticket.messages.filter(direction=Message.DIRECTION_OUTBOUND).first()
        if out:
            self.assertNotIn("{tracking_url}", out.body_text)   # never the raw placeholder

    def test_info_category_unresolved_autoreplies_with_ai(self):
        # Same scenario WITH an AI responder -> auto-reply & close (Route A): no ticket.
        from apps.classifier import service as classifier

        class FakeProvider:
            def generate_text(self, system, user):
                return "Your order is on the way -- track it in the DeoDap app under Orders."

        ticket = self.make_ticket(self.s12)
        orig = classifier.build_provider
        classifier.build_provider = lambda s: FakeProvider()
        try:
            engine.run(ticket)
        finally:
            classifier.build_provider = orig
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_AUTO_RESOLVED)   # closed, no ticket
        out = ticket.messages.get(direction=Message.DIRECTION_OUTBOUND)
        self.assertFalse(out.is_draft)
        self.assertIn("track it", out.body_text)

    def test_info_only_with_live_context_auto_sends(self):
        ticket = self.make_ticket(self.s12, extracted={"tracking_url": "http://t/123"})
        plan = engine.run(ticket)
        self.assertEqual(plan.send_mode, engine.AUTO)
        sent = ticket.messages.get(direction=Message.DIRECTION_OUTBOUND)
        self.assertIn("http://t/123", sent.body_text)

    def test_toggle_off_disables_autosend(self):
        self.settings.automation_toggles = {Rule.ACTION_INFO_ONLY: "off"}
        self.settings.save()
        ticket = self.make_ticket(self.s121)
        plan = engine.run(ticket)
        self.assertEqual(plan.send_mode, engine.NONE)

    def test_toggle_draft_drafts(self):
        self.settings.automation_toggles = {Rule.ACTION_INFO_ONLY: "draft"}
        self.settings.save()
        ticket = self.make_ticket(self.s121)
        plan = engine.run(ticket)
        self.assertEqual(plan.send_mode, engine.DRAFT)

    def test_needs_live_data_drafts(self):
        ticket = self.make_ticket(self.s11, extracted={"order_id": "DD1"})
        plan = engine.run(ticket)
        self.assertIn("needs_live_data", plan.reasons)
        self.assertEqual(plan.send_mode, engine.DRAFT)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_AWAITING_AGENT)


class EvidenceTests(BaseFixture):
    def test_no_evidence_auto_sends_request(self):
        ticket = self.make_ticket(self.s33, extracted={"order_id": "DD1"})
        plan = engine.run(ticket)
        self.assertEqual(plan.action_code, Rule.ACTION_AWAIT_EVIDENCE)
        self.assertEqual(plan.send_mode, engine.AUTO)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_AWAITING_EVIDENCE)
        sent = ticket.messages.get(direction=Message.DIRECTION_OUTBOUND)
        self.assertFalse(sent.is_draft)

    def test_evidence_present_drafts_for_agent(self):
        ticket = self.make_ticket(self.s33, extracted={"order_id": "DD1", "has_photo": True})
        plan = engine.run(ticket)
        self.assertEqual(plan.action_code, Rule.ACTION_CREATE_TICKET)
        self.assertEqual(plan.send_mode, engine.DRAFT)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_AWAITING_AGENT)

    def test_missing_mandatory_input_asks_instead(self):
        ticket = self.make_ticket(self.s33, extracted={})  # no order_id
        plan = engine.run(ticket)
        self.assertEqual(plan.action_code, "evidence_request")
        self.assertTrue(any(r.startswith("missing_inputs") for r in plan.reasons))
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_AWAITING_EVIDENCE)


class GuardrailTests(BaseFixture):
    def test_sensitive_always_human_high_escalated(self):
        ticket = self.make_ticket(self.s162)
        plan = engine.run(ticket)
        self.assertIn("sensitive_subtopic", plan.reasons)
        self.assertNotEqual(plan.send_mode, engine.AUTO)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_ESCALATED)
        self.assertEqual(ticket.priority, Ticket.PRIORITY_HIGH)
        self.assertTrue(ticket.audit_log.filter(event="agent_task").exists())

    def test_low_confidence_drafts(self):
        ticket = self.make_ticket(self.s121, confidence=0.5)
        plan = engine.run(ticket)
        self.assertIn("low_confidence", plan.reasons)
        self.assertEqual(plan.send_mode, engine.DRAFT)

    def test_angry_escalates(self):
        ticket = self.make_ticket(self.s121, sentiment="angry")
        plan = engine.run(ticket)
        self.assertIn("angry_sentiment", plan.reasons)
        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, Ticket.PRIORITY_HIGH)
        self.assertEqual(ticket.status, Ticket.STATUS_ESCALATED)

    def test_uncategorized_routes_to_agent(self):
        ticket = self.make_ticket(None)
        plan = engine.run(ticket)
        self.assertIn("uncategorized", plan.reasons)
        self.assertEqual(plan.send_mode, engine.DRAFT)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_AWAITING_AGENT)

    def test_ignored_ticket_is_noop(self):
        ticket = self.make_ticket(self.s121)
        ticket.is_ignored = True
        ticket.save()
        self.assertIsNone(engine.run(ticket))


class TriggerCrpTests(BaseFixture):
    def test_trigger_crp_high_escalated_agent_task(self):
        ticket = self.make_ticket(self.s61)
        plan = engine.run(ticket)
        self.assertEqual(plan.action_code, Rule.ACTION_TRIGGER_CRP)
        self.assertEqual(plan.send_mode, engine.DRAFT)
        self.assertTrue(plan.create_agent_task)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_ESCALATED)
        self.assertEqual(ticket.priority, Ticket.PRIORITY_HIGH)


class SlaTests(BaseFixture):
    def test_high_priority_sets_sla(self):
        ticket = self.make_ticket(self.s61)
        engine.run(ticket)
        ticket.refresh_from_db()
        self.assertIsNotNone(ticket.sla_due_at)

    def test_sla_config_override(self):
        self.settings.sla_config = {"6": {"first_response_mins": 30}}
        self.settings.save()
        ticket = self.make_ticket(self.s61)
        engine.run(ticket)
        ticket.refresh_from_db()
        self.assertIsNotNone(ticket.sla_due_at)


class DecideEndpointTests(BaseFixture):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("agent", password="pw")
        self.org.members.add(self.user)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_decide_action_applies_engine(self):
        ticket = self.make_ticket(self.s121)
        resp = self.api.post(f"/api/tickets/{ticket.id}/decide/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Ticket.STATUS_AUTO_RESOLVED)

    def test_decide_action_on_ignored_400(self):
        ticket = self.make_ticket(self.s121)
        ticket.is_ignored = True
        ticket.save()
        resp = self.api.post(f"/api/tickets/{ticket.id}/decide/")
        self.assertEqual(resp.status_code, 400)


class PipelineIntegrationTests(BaseFixture):
    def test_sync_history_classifies_then_decides(self):
        from apps.classifier import service as classifier
        from apps.classifier.tests import FakeProvider
        from apps.ingestion import service as ingestion
        from apps.ingestion.tests import FakeGmailClient, gmail_raw

        # Classifier maps the mail to 12.1 -> engine should auto-send + auto-resolve.
        self.settings.ai_api_key = "k"
        self.settings.save()
        provider = FakeProvider({
            "category": "12. Delivery Coverage & Shipping",
            "sub_topic": "12.1 Pincode / Serviceability",
            "confidence": 0.95, "extracted": {}, "language": "en", "sentiment": "neutral",
        })
        original = classifier.build_provider
        classifier.build_provider = lambda settings: provider
        try:
            client = FakeGmailClient([gmail_raw(text="do you deliver to 390001?")])
            ingestion.sync_history(self.mailbox, client=client)
        finally:
            classifier.build_provider = original

        ticket = Ticket.objects.get(thread_id="t1")
        self.assertEqual(ticket.sub_topic_ref, self.s121)
        self.assertEqual(ticket.status, Ticket.STATUS_AUTO_RESOLVED)
        self.assertTrue(
            ticket.messages.filter(direction=Message.DIRECTION_OUTBOUND).exists()
        )
