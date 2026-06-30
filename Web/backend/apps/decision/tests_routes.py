"""
Tests for the four-route action router (Mail Flow §4): A auto-answer & close,
B evidence -> ticket, C direct ticket, D human-first.

    python manage.py test apps.decision.tests_routes
"""

from django.test import TestCase

from apps.decision import engine
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, Rule, SubTopic
from apps.tickets.models import Ticket


class RouteRouterTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")

    def _ticket(self, *, sub=None, cat=None):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="hi",
            category_ref=cat or self.cat, sub_topic_ref=sub,
        )

    def _plan(self, **kw):
        kw.setdefault("action_code", "create_ticket")
        kw.setdefault("action_label", "x")
        return engine.DecisionPlan(**kw)

    def test_route_a_auto_answer_and_close(self):
        t = self._ticket()
        plan = self._plan(ai_handled=True, status=Ticket.STATUS_AUTO_RESOLVED)
        self.assertEqual(engine.route_for(t, plan)[0], "A")

    def test_route_d_low_confidence(self):
        t = self._ticket()
        plan = self._plan(status=Ticket.STATUS_AWAITING_AGENT, reasons=["low_confidence"])
        self.assertEqual(engine.route_for(t, plan)[0], "D")

    def test_route_d_sensitive_subtopic(self):
        sub = SubTopic.objects.create(category=self.cat, code="3.9", name="Fraud",
                                      is_sensitive=True)
        t = self._ticket(sub=sub)
        plan = self._plan(status=Ticket.STATUS_ESCALATED)
        self.assertEqual(engine.route_for(t, plan)[0], "D")

    def test_route_b_evidence_subtopic(self):
        sub = SubTopic.objects.create(category=self.cat, code="3.3", name="Damaged",
                                      requires_evidence=True)
        t = self._ticket(sub=sub)
        plan = self._plan(action_code=Rule.ACTION_AWAIT_EVIDENCE,
                          status=Ticket.STATUS_AWAITING_EVIDENCE)
        self.assertEqual(engine.route_for(t, plan)[0], "B")

    def test_route_b_video_category(self):
        cat = Category.objects.create(brand=self.brand, code="7", name="Return/Refund",
                                      requires_video=True)
        t = self._ticket(cat=cat)
        plan = self._plan(status=Ticket.STATUS_AWAITING_AGENT)
        self.assertEqual(engine.route_for(t, plan)[0], "B")

    def test_route_c_direct_ticket(self):
        sub = SubTopic.objects.create(category=self.cat, code="3.2", name="Delayed")
        t = self._ticket(sub=sub)
        plan = self._plan(action_code="create_ticket", status=Ticket.STATUS_AWAITING_AGENT)
        self.assertEqual(engine.route_for(t, plan)[0], "C")

    def test_route_stamped_on_ticket_and_audit(self):
        sub = SubTopic.objects.create(category=self.cat, code="3.2", name="Delayed")
        t = self._ticket(sub=sub)
        engine.run(t)
        t.refresh_from_db()
        self.assertIn(t.extracted.get("route"), {"A", "B", "C", "D"})
        self.assertTrue(t.audit_log.filter(event="decision").exists())
        decision = t.audit_log.filter(event="decision").last()
        self.assertIn("route", decision.detail)
