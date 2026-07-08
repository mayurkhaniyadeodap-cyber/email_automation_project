"""
Manager dashboard + reporting layer (ADDITIVE). Logging, employee performance, dashboard
counts, report generation, and CSV / Excel / PDF export.

    python manage.py test apps.analytics.tests_dashboard
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.analytics import dashboard as dash
from apps.analytics import exports
from apps.analytics.logging import log_auto_reply, log_manual_reply
from apps.analytics.models import AutoReplyLog, ManualReplyLog
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Escalation, Ticket


class DashboardBase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.agent = get_user_model().objects.create_superuser(
            "rahul", "rahul@x.com", "x", first_name="Rahul")
        self.agent.organizations.add(self.org)
        self.client = APIClient()
        self.client.force_authenticate(self.agent)

    def _ticket(self, **kw):
        return Ticket.objects.create(organization=self.org, brand=self.brand, mailbox=self.mailbox,
                                     customer_email="c@x.com", subject="hi", **kw)


class LoggingTests(DashboardBase):
    def test_manual_reply_logged(self):
        t = self._ticket()
        log_manual_reply(brand=self.brand, employee=self.agent, customer_email="c@x.com",
                         subject="re: hi", message_id="<m1>", thread_id="<t1>", ticket=t,
                         body="hello there", attachments=2, response_seconds=120)
        row = ManualReplyLog.objects.get()
        self.assertEqual(row.employee_email, "rahul@x.com")
        self.assertEqual(row.employee_name, "Rahul")
        self.assertEqual(row.customer_email, "c@x.com")
        self.assertEqual(row.message_id, "<m1>")
        self.assertEqual(row.thread_id, "<t1>")
        self.assertEqual(row.ticket_ref, t.ticket_id)
        self.assertEqual(row.reply_size, len("hello there"))
        self.assertEqual(row.attachments, 2)

    def test_auto_reply_logged(self):
        t = self._ticket()
        log_auto_reply(brand=self.brand, customer_email="c@x.com", subject="created",
                       template="M5", trigger="confirmation_created", ticket=t, execution_ms=12)
        row = AutoReplyLog.objects.get()
        self.assertEqual(row.template, "M5")
        self.assertEqual(row.trigger, "confirmation_created")
        self.assertTrue(row.success)
        self.assertEqual(row.ticket_ref, t.ticket_id)

    def test_auto_reply_logged_via_confirmation(self):
        # send_confirmation must log an AutoReplyLog (end-to-end automated reply).
        from apps.ingestion import service
        t = self._ticket(category="1. Shipment", sub_topic="Tracking", ticket_number="TKT-1")
        service.send_confirmation(t, "created")
        self.assertTrue(AutoReplyLog.objects.filter(ticket=t).exists())


class EmployeePerformanceTests(DashboardBase):
    def test_employee_statistics(self):
        t = self._ticket()
        for i in range(3):
            log_manual_reply(brand=self.brand, employee=self.agent, customer_email="c@x.com",
                             subject="r", message_id=f"<m{i}>", ticket=t, body="x",
                             response_seconds=60)
        Escalation.objects.create(organization=self.org, brand=self.brand, sender="c@x.com",
                                  matched_keyword="LAWYER", resolved_by="rahul@x.com",
                                  status=Escalation.STATUS_RESOLVED)
        perf = dash.employee_performance([self.brand.id])
        me = next(r for r in perf if r["employee_email"] == "rahul@x.com")
        self.assertEqual(me["manual_replies"], 3)
        self.assertEqual(me["avg_response_seconds"], 60)
        self.assertEqual(me["escalations_handled"], 1)
        self.assertIsNotNone(me["last_active"])           # touched on each reply

    def test_agent_performance(self):
        log_manual_reply(brand=self.brand, employee=self.agent, customer_email="c@x.com",
                         subject="r", message_id="<m>", body="x")
        resp = self.client.get("/api/analytics/employee-performance/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(r["employee_email"] == "rahul@x.com" for r in resp.data))


class DashboardCountTests(DashboardBase):
    def test_dashboard_counts(self):
        self._ticket()
        self._ticket(is_ignored=True)
        Escalation.objects.create(organization=self.org, brand=self.brand, sender="c@x.com",
                                  matched_keyword="NCH", status=Escalation.STATUS_MANUAL_REVIEW)
        log_auto_reply(brand=self.brand, customer_email="c@x.com", template="M5")
        log_manual_reply(brand=self.brand, employee=self.agent, customer_email="c@x.com", body="x")
        resp = self.client.get("/api/analytics/dashboard/")
        self.assertEqual(resp.status_code, 200)
        s = resp.data["summary"]
        self.assertEqual(s["total_tickets"]["total"], 2)
        self.assertEqual(s["ignored_emails"]["total"], 1)
        self.assertEqual(s["escalations"]["total"], 1)
        self.assertEqual(s["auto_replies"]["total"], 1)
        self.assertEqual(s["manual_replies"]["total"], 1)
        self.assertEqual(s["pending_manual_review"]["total"], 1)
        self.assertIn("series", resp.data)
        self.assertIn("scoreboard", resp.data)


class ReportTests(DashboardBase):
    def setUp(self):
        super().setUp()
        t = self._ticket(ticket_number="TKT-9")
        log_manual_reply(brand=self.brand, employee=self.agent, customer_email="cust@x.com",
                         subject="re: issue", message_id="<m>", ticket=t, body="reply", attachments=1)
        log_auto_reply(brand=self.brand, customer_email="cust@x.com", subject="created",
                       template="M5", trigger="confirmation_created", ticket=t)

    def test_reports_generate(self):
        m = self.client.get("/api/analytics/manual-replies/")
        self.assertEqual(m.status_code, 200)
        self.assertEqual(m.data["count"], 1)
        self.assertEqual(m.data["results"][0]["employee_email"], "rahul@x.com")
        a = self.client.get("/api/analytics/auto-replies/")
        self.assertEqual(a.data["count"], 1)
        self.assertEqual(a.data["results"][0]["template"], "M5")

    def test_export_csv(self):
        r = self.client.get("/api/analytics/manual-replies/?export=csv")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "text/csv")
        self.assertIn(b"employee_email", r.content)
        self.assertIn(b"rahul@x.com", r.content)

    def test_export_excel(self):
        r = self.client.get("/api/analytics/manual-replies/?export=xlsx")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])
        self.assertTrue(r.content[:2] == b"PK")          # xlsx is a zip
        self.assertIn(".xlsx", r["Content-Disposition"])

    def test_export_pdf(self):
        r = self.client.get("/api/analytics/auto-replies/?export=pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_export_helper_empty_rows(self):
        # exports must not crash on zero rows.
        self.assertEqual(exports.to_csv([], "x").status_code, 200)
        self.assertEqual(exports.to_excel([], "x").status_code, 200)
        self.assertEqual(exports.to_pdf([], "x").status_code, 200)


from datetime import timedelta  # noqa: E402


class TicketTrendTests(DashboardBase):
    """Dynamic Ticket Trend endpoint: GET /api/dashboard/ticket-trend?range=week|month|year."""

    def _at(self, days_ago):
        t = self._ticket()
        Ticket.objects.filter(pk=t.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
        return t

    def test_week_range(self):
        self._at(0); self._at(0); self._at(3)                       # 2 today, 1 three days ago
        r = self.client.get("/api/dashboard/ticket-trend?range=week")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data["labels"]), 7)
        self.assertEqual(len(r.data["values"]), 7)
        self.assertEqual(sum(r.data["values"]), 3)
        self.assertEqual(r.data["values"][-1], 2)                   # today = last bucket
        self.assertIn(r.data["labels"][0], ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])

    def test_month_range(self):
        self._at(0); self._at(10)
        r = self.client.get("/api/dashboard/ticket-trend?range=month")
        self.assertEqual(len(r.data["labels"]), 30)
        self.assertEqual(len(r.data["values"]), 30)
        self.assertEqual(sum(r.data["values"]), 2)
        self.assertTrue(all(l.isdigit() for l in r.data["labels"]))  # day-of-month labels

    def test_year_range(self):
        self._at(0); self._at(45)                                   # this month + ~1-2 months ago
        r = self.client.get("/api/dashboard/ticket-trend?range=year")
        self.assertEqual(len(r.data["labels"]), 12)
        self.assertEqual(len(r.data["values"]), 12)
        self.assertEqual(sum(r.data["values"]), 2)
        self.assertEqual(r.data["labels"][-1], dash._MONTH_ABBR[timezone.now().month - 1])

    def test_default_and_invalid_range(self):
        self.assertEqual(len(self.client.get("/api/dashboard/ticket-trend").data["labels"]), 7)
        self.assertEqual(
            len(self.client.get("/api/dashboard/ticket-trend?range=bogus").data["labels"]), 7)

    def test_requires_auth(self):
        self.assertIn(APIClient().get("/api/dashboard/ticket-trend").status_code, (401, 403))
