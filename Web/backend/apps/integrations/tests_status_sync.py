"""Care Panel -> Mail Engine ticket STATUS SYNCHRONIZATION (polling job + shared mapping).

    python manage.py test apps.integrations.tests_status_sync
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.integrations import care_panel_status
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Ticket


class FakeClient:
    """Stub of CarePanelClient.lookup returning a canned open-tickets response."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def lookup(self, *, phone=None, email=None, order_id=None):
        self.calls.append(phone)
        return self.response


class StatusMapTests(TestCase):
    def test_supported_statuses_map_correctly(self):
        m = care_panel_status.normalize
        for s in ("Open", "In Progress", "In-process", "Awaiting Customer", "Pending", "Reopened"):
            self.assertEqual(m(s), Ticket.STATUS_IN_PROGRESS, s)
        self.assertEqual(m("Resolved"), Ticket.STATUS_RESOLVED)
        self.assertEqual(m("Closed"), Ticket.STATUS_CLOSED)
        self.assertIsNone(m("something else"))

    def test_additional_care_panel_statuses(self):
        m = care_panel_status.normalize
        # Active holds -> In Progress.
        for s in ("Hold Waiting For Customer", "Hold Waiting For Others",
                  "Waiting for Courier Update"):
            self.assertEqual(m(s), Ticket.STATUS_IN_PROGRESS, s)
        # Terminal closure reasons + Duplicate -> Closed (hyphen/space variants both match).
        for s in ("Closed - No Response", "Closed No Response", "Closed No Solution",
                  "Closed With Solution", "Duplicate"):
            self.assertEqual(m(s), Ticket.STATUS_CLOSED, s)


class PollSyncTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self, *, cpid="HASH1", phone="9876543210", status=Ticket.STATUS_IN_PROGRESS,
                age_min=60):
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="help", status=status,
            extracted={"care_panel_ticket_id": cpid, "phone": phone})
        Ticket.objects.filter(pk=t.pk).update(created_at=timezone.now() - timedelta(minutes=age_min))
        t.refresh_from_db()
        return t

    def _run(self, response, **kw):
        client = FakeClient(response)
        result = care_panel_status.sync_statuses_from_care_panel(
            client_for=lambda brand: client, **kw)
        return result, client

    # 1) Agent CLOSED it in the panel -> gone from the open list -> we mark CLOSED (the reported bug).
    def test_closed_in_panel_is_mirrored(self):
        t = self._ticket(cpid="HASH1")
        (checked, updated, closed), client = self._run(
            {"success": True, "hasTickets": False, "tickets": []})
        t.refresh_from_db()
        self.assertEqual(t.status, Ticket.STATUS_CLOSED)
        self.assertIsNotNone(t.resolved_at)
        self.assertEqual((checked, closed), (1, 1))
        self.assertTrue(t.audit_log.filter(event="status_mirrored", detail__to="closed").exists())
        self.assertEqual(client.calls, ["9876543210"])           # one lookup

    # 2) Still open in the panel with a new status -> mirror it.
    def test_open_status_mirrored(self):
        t = self._ticket(cpid="HASH1", status=Ticket.STATUS_AWAITING_AGENT)
        self._run({"success": True, "hasTickets": True, "tickets": [{"id": "HASH1", "status": "In-process"}]})
        t.refresh_from_db()
        self.assertEqual(t.status, Ticket.STATUS_IN_PROGRESS)

    # 3) Panel Resolved -> RESOLVED (exact terminal mapping when present in the feed).
    def test_resolved_status_mirrored(self):
        t = self._ticket(cpid="HASH1")
        self._run({"success": True, "hasTickets": True, "tickets": [{"id": "HASH1", "status": "Resolved"}]})
        t.refresh_from_db()
        self.assertEqual(t.status, Ticket.STATUS_RESOLVED)

    # 4) Same status -> NO update, NO duplicate audit (avoid duplicate updates).
    def test_no_change_no_duplicate(self):
        t = self._ticket(cpid="HASH1", status=Ticket.STATUS_IN_PROGRESS)
        (checked, updated, closed), _ = self._run(
            {"success": True, "hasTickets": True, "tickets": [{"id": "HASH1", "status": "In Progress"}]})
        self.assertEqual((updated, closed), (0, 0))
        self.assertFalse(t.audit_log.filter(event="status_mirrored").exists())

    # 5) Idempotent: once closed it is terminal and excluded from the next run.
    def test_idempotent_second_run_noop(self):
        self._ticket(cpid="HASH1")
        self._run({"success": True, "hasTickets": False, "tickets": []})   # -> closed
        (checked, updated, closed), _ = self._run({"success": True, "hasTickets": False, "tickets": []})
        self.assertEqual((checked, updated, closed), (0, 0, 0))            # nothing active to check

    # 6) Grace period: a brand-new ticket is skipped.
    def test_grace_period_skips_new_ticket(self):
        self._ticket(cpid="HASH1", age_min=2)
        (checked, _, _), _ = self._run({"success": True, "hasTickets": False, "tickets": []},
                                       grace_minutes=10)
        self.assertEqual(checked, 0)

    # 7) No phone / no Care Panel id -> skipped (can't query the phone-keyed API).
    def test_no_phone_skipped(self):
        self._ticket(cpid="HASH1", phone="")
        (checked, _, _), client = self._run({"success": True, "hasTickets": False, "tickets": []})
        self.assertEqual(checked, 0)
        self.assertEqual(client.calls, [])

    # 8) A failed / not-ok lookup NEVER closes tickets (safety).
    def test_api_not_ok_does_not_close(self):
        t = self._ticket(cpid="HASH1")
        (checked, updated, closed), _ = self._run({"success": False})
        t.refresh_from_db()
        self.assertEqual(t.status, Ticket.STATUS_IN_PROGRESS)             # untouched
        self.assertEqual(closed, 0)

    # 9) Grouping: two tickets, same phone -> ONE lookup; present one updated, absent one closed.
    def test_grouped_lookup_mixed_outcomes(self):
        t_open = self._ticket(cpid="OPEN1", status=Ticket.STATUS_AWAITING_AGENT)
        t_closed = self._ticket(cpid="GONE2")
        _, client = self._run({"success": True, "hasTickets": True,
                               "tickets": [{"id": "OPEN1", "status": "In-process"}]})
        t_open.refresh_from_db(); t_closed.refresh_from_db()
        self.assertEqual(t_open.status, Ticket.STATUS_IN_PROGRESS)
        self.assertEqual(t_closed.status, Ticket.STATUS_CLOSED)
        self.assertEqual(len(client.calls), 1)                            # grouped by phone

    # 10) Every synchronization event is logged.
    def test_events_are_logged(self):
        self._ticket(cpid="HASH1")
        with self.assertLogs("apps.integrations.care_panel_status", level="INFO") as cm:
            self._run({"success": True, "hasTickets": False, "tickets": []})
        blob = "\n".join(cm.output)
        self.assertIn("CARE_PANEL_STATUS_SYNC", blob)
        self.assertIn("CARE_PANEL_STATUS_SYNC_DONE", blob)
