"""
Offline tests for Phase 6 analytics + evidence handling (doc section 13):
volume / SLA / AI-accuracy / agent-performance reports, the scoped endpoints,
the agent correction (accuracy ground truth) and the attachments endpoint.

    python manage.py test apps.analytics
"""

from datetime import timedelta
from django.contrib.auth import get_user_model  # type: ignore[reportMissingModuleSource]
from django.test import TestCase  # type: ignore[reportMissingModuleSource]
from django.utils import timezone  # type: ignore[reportMissingModuleSource]
from rest_framework.test import APIClient  # type: ignore[reportMissingImports]

from apps.analytics import reports
from apps.brand_settings.models import BrandSettings
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, SubTopic
from apps.tickets.models import AuditLogEntry, Message, Ticket

User = get_user_model()


class BaseFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand)
        self.cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        self.sub = SubTopic.objects.create(category=self.cat, code="3.3", name="Damaged",
                                           mandatory_inputs=["order_id"])
        self.user = User.objects.create_user("agent", password="pw")
        self.org.members.add(self.user)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def mk(self, *, status=Ticket.STATUS_NEW, priority=Ticket.PRIORITY_NORMAL,
           sla_due_at=None, ai_confidence=None, ai_handled=False, ignored=False,
           sub=None, category="", attachments=None):
        ticket = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="s", status=status, priority=priority,
            sla_due_at=sla_due_at, ai_confidence=ai_confidence, ai_handled=ai_handled,
            is_ignored=ignored, sub_topic_ref=sub, category=category,
        )
        Message.objects.create(
            ticket=ticket, direction=Message.DIRECTION_INBOUND, from_email="b@x.com",
            subject="s", body_text="hi", attachments=attachments or [],
        )
        return ticket


class VolumeReportTests(BaseFixture):
    def test_counts_by_status_and_open(self):
        self.mk(status=Ticket.STATUS_AUTO_RESOLVED, category="3. Delivery Issues")
        self.mk(status=Ticket.STATUS_AWAITING_AGENT, category="3. Delivery Issues")
        self.mk(ignored=True)
        rep = reports.volume_report(Ticket.objects.filter(brand=self.brand))
        self.assertEqual(rep["total"], 3)
        self.assertEqual(rep["ignored"], 1)
        self.assertEqual(rep["auto_resolved"], 1)
        self.assertEqual(rep["open"], 1)  # awaiting_agent only (auto_resolved/ignored excluded)
        self.assertEqual(rep["by_category"]["3. Delivery Issues"], 2)


class SlaReportTests(BaseFixture):
    def test_breached_due_soon_and_compliance(self):
        now = timezone.now()
        self.mk(status=Ticket.STATUS_AWAITING_AGENT, sla_due_at=now - timedelta(hours=1))
        self.mk(status=Ticket.STATUS_AWAITING_AGENT, sla_due_at=now + timedelta(minutes=30))
        # met: resolved before a future due date
        self.mk(status=Ticket.STATUS_RESOLVED, sla_due_at=now + timedelta(hours=5))
        # missed: resolved after a past due date
        self.mk(status=Ticket.STATUS_RESOLVED, sla_due_at=now - timedelta(hours=5))

        rep = reports.sla_report(Ticket.objects.filter(brand=self.brand), now=now)
        self.assertEqual(rep["breached"], 1)
        self.assertEqual(rep["due_soon"], 1)
        self.assertEqual(rep["met"], 1)
        self.assertEqual(rep["missed"], 1)
        self.assertEqual(rep["compliance_rate"], 0.5)

    def test_resolved_at_stamped_on_terminal(self):
        t = self.mk(status=Ticket.STATUS_NEW)
        self.assertIsNone(t.resolved_at)
        t.status = Ticket.STATUS_RESOLVED
        t.save()
        t.refresh_from_db()
        self.assertIsNotNone(t.resolved_at)
        # Reopening clears it.
        t.status = Ticket.STATUS_IN_PROGRESS
        t.save()
        t.refresh_from_db()
        self.assertIsNone(t.resolved_at)


class AiAccuracyReportTests(BaseFixture):
    def test_accuracy_counts_corrections(self):
        t1 = self.mk(ai_confidence=0.9, ai_handled=True, sub=self.sub)
        self.mk(ai_confidence=0.6, sub=self.sub)
        self.mk(ai_confidence=0.95)  # uncategorized (no sub ref)
        AuditLogEntry.objects.create(ticket=t1, actor="agent", event="correction", detail={})

        rep = reports.ai_accuracy_report(Ticket.objects.filter(brand=self.brand))
        self.assertEqual(rep["classified"], 3)
        self.assertEqual(rep["auto_handled"], 1)
        self.assertEqual(rep["uncategorized"], 1)
        self.assertEqual(rep["low_confidence"], 1)  # 0.6 < 0.75
        self.assertEqual(rep["corrections"], 1)
        self.assertAlmostEqual(rep["accuracy_rate"], round(1 - 1 / 3, 4))


class AgentPerformanceTests(BaseFixture):
    def test_groups_events_by_actor(self):
        t = self.mk()
        AuditLogEntry.objects.create(ticket=t, actor="agent", event="reply_sent", detail={})
        AuditLogEntry.objects.create(ticket=t, actor="agent", event="correction", detail={})
        AuditLogEntry.objects.create(ticket=t, actor="ai", event="classified", detail={})
        AuditLogEntry.objects.create(ticket=t, actor="system", event="ticket_created", detail={})

        rep = reports.agent_performance_report(Ticket.objects.filter(brand=self.brand))
        self.assertIn("agent", rep)
        self.assertNotIn("ai", rep)
        self.assertNotIn("system", rep)
        self.assertEqual(rep["agent"]["total"], 2)
        self.assertEqual(rep["agent"]["reply_sent"], 1)


class EndpointTests(BaseFixture):
    def test_overview_endpoint(self):
        self.mk(ai_confidence=0.9)
        resp = self.api.get("/api/analytics/overview/?brand=%s" % self.brand.id)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("volume", resp.data)
        self.assertIn("sla", resp.data)
        self.assertIn("ai", resp.data)
        self.assertIn("agents", resp.data)

    def test_scoping_excludes_other_orgs(self):
        other_org = Organization.objects.create(name="Other")
        other_brand = Brand.objects.create(organization=other_org, name="OB")
        Ticket.objects.create(organization=other_org, brand=other_brand,
                              customer_email="x@y.com", subject="x")
        self.mk()
        resp = self.api.get("/api/analytics/volume/")
        self.assertEqual(resp.data["total"], 1)  # only the caller's org


class CorrectionActionTests(BaseFixture):
    def test_correct_reclassifies_and_logs(self):
        ticket = self.mk(ai_confidence=0.9, category="Uncategorized")
        resp = self.api.post(
            f"/api/tickets/{ticket.id}/correct/",
            {"sub_topic_ref": self.sub.id}, format="json",
        )
        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.sub_topic_ref, self.sub)
        self.assertEqual(ticket.sub_topic, "3.3 Damaged")
        self.assertTrue(ticket.audit_log.filter(event="correction").exists())

    def test_correct_rejects_foreign_subtopic(self):
        other_org = Organization.objects.create(name="Other")
        other_brand = Brand.objects.create(organization=other_org, name="OB")
        other_cat = Category.objects.create(brand=other_brand, code="1", name="X")
        other_sub = SubTopic.objects.create(category=other_cat, code="1.1", name="Y")
        ticket = self.mk()
        resp = self.api.post(
            f"/api/tickets/{ticket.id}/correct/",
            {"sub_topic_ref": other_sub.id}, format="json",
        )
        self.assertEqual(resp.status_code, 404)


class AttachmentsEndpointTests(BaseFixture):
    def test_lists_attachments_and_evidence(self):
        ticket = self.mk(attachments=[
            {"filename": "proof.jpg", "mime_type": "image/jpeg", "attachment_id": "a1"},
            {"filename": "clip.mp4", "mime_type": "video/mp4", "attachment_id": "a2"},
        ])
        resp = self.api.get(f"/api/tickets/{ticket.id}/attachments/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["count"], 2)
        self.assertTrue(resp.data["evidence"]["has_photo"])
        self.assertTrue(resp.data["evidence"]["has_unboxing_video"])
