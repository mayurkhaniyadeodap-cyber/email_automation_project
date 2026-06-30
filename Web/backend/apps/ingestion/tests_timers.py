"""
Tests for waiting-state timers (Mail Flow §8): 24h reminder (M7R), 72h auto-close
(M7C), and 7-day reopen.

    python manage.py test apps.ingestion.tests_timers
"""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.ingestion import service, timers
from apps.ingestion.tests_imap import FakeImap
from apps.ingestion.tests_smart import eml
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import PendingConversation, Ticket


class WaitingTimerTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _pending(self, *, status="awaiting_evidence", age_hours=0, email="b@x.com",
                 original_id="<a@x>"):
        p = PendingConversation.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email=email, subject="damaged", status=status,
            original_message_id=original_id, last_message_id=original_id,
        )
        if age_hours:
            PendingConversation.objects.filter(pk=p.pk).update(
                created_at=timezone.now() - timedelta(hours=age_hours))
            p.refresh_from_db()
        return p

    def test_reminder_after_24h_only_once(self):
        p = self._pending(age_hours=25)
        reminded, closed = timers.sweep_waiting_states()
        self.assertEqual((reminded, closed), (1, 0))
        p.refresh_from_db()
        self.assertIsNotNone(p.reminder_sent_at)
        # A second sweep does NOT re-send the reminder.
        self.assertEqual(timers.sweep_waiting_states(), (0, 0))

    def test_no_reminder_before_24h(self):
        self._pending(age_hours=5)
        self.assertEqual(timers.sweep_waiting_states(), (0, 0))

    def test_autoclose_after_72h(self):
        p = self._pending(age_hours=73)
        reminded, closed = timers.sweep_waiting_states()
        self.assertEqual(closed, 1)
        p.refresh_from_db()
        self.assertEqual(p.status, "closed")
        self.assertIsNotNone(p.closed_at)

    def test_waiting_for_video_state_also_swept(self):
        self._pending(status="waiting_for_video", age_hours=73)
        self.assertEqual(timers.sweep_waiting_states()[1], 1)

    def test_reply_within_7_days_reopens(self):
        # Auto-closed 2 days ago.
        p = self._pending(status="awaiting_evidence")
        PendingConversation.objects.filter(pk=p.pk).update(
            status="closed", closed_at=timezone.now() - timedelta(days=2))
        # Customer replies on the same thread -> reopened (still no ticket).
        reply = eml(subject="Re: damaged", body="sorry, my order is DD9999",
                    message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>")
        service.fetch_imap(self.mailbox, client=FakeImap([reply]))
        p.refresh_from_db()
        self.assertNotEqual(p.status, "closed")           # revived
        self.assertIsNone(p.closed_at)
        self.assertEqual(Ticket.objects.count(), 0)        # still gathering evidence

    def test_reply_after_7_days_starts_fresh(self):
        # Auto-closed 10 days ago -> beyond the reopen window.
        p = self._pending(status="awaiting_evidence")
        PendingConversation.objects.filter(pk=p.pk).update(
            status="closed", closed_at=timezone.now() - timedelta(days=10))
        self.assertIsNone(service._find_pending(self.brand, {
            "in_reply_to": "<a@x>", "references": ["<a@x>"], "from_email": "b@x.com"}))
