"""
Unread-count endpoints that drive the sidebar badges + new-item toasts for BOTH the
Escalation and Internal Communications modules.

    python manage.py test apps.tickets.tests_escalation_unread
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Escalation, InternalEmail

User = get_user_model()


class EscalationUnreadCountTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.user = User.objects.create_user("agent", password="pw")
        self.org.members.add(self.user)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def _esc(self, *, is_read=False, status=Escalation.STATUS_MANUAL_REVIEW, subject="legal"):
        return Escalation.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            sender="buyer@example.com", subject=subject, matched_keyword="consumer court",
            status=status, is_read=is_read)

    def _count(self):
        r = self.api.get(f"/api/escalations/unread_count/?organization={self.org.id}&brand={self.brand.id}")
        self.assertEqual(r.status_code, 200)
        return r.json()["count"]

    def test_counts_only_unread_non_terminal(self):
        self._esc()                                             # unread -> counts
        self._esc()                                             # unread -> counts
        self._esc(is_read=True)                                 # read -> excluded
        self._esc(status=Escalation.STATUS_RESOLVED)            # terminal -> excluded
        self._esc(status=Escalation.STATUS_IGNORED)             # terminal -> excluded
        self.assertEqual(self._count(), 2)

    def test_zero_when_none(self):
        self.assertEqual(self._count(), 0)

    def test_opening_detail_marks_read_and_drops_count(self):
        esc = self._esc()
        self.assertEqual(self._count(), 1)
        # Opening the detail marks it read (existing helpdesk behaviour).
        r = self.api.get(f"/api/escalations/{esc.id}/?organization={self.org.id}&brand={self.brand.id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._count(), 0)                      # badge clears after read

    def test_scoped_to_user_orgs(self):
        # An escalation in another org the user does NOT belong to is never counted.
        other = Organization.objects.create(name="Other")
        ob = Brand.objects.create(organization=other, name="Other.in")
        Escalation.objects.create(organization=other, brand=ob, sender="x@y.com",
                                  status=Escalation.STATUS_MANUAL_REVIEW, is_read=False)
        self._esc()
        self.assertEqual(self._count(), 1)                      # only this org's unread

    def test_returns_items_for_toast(self):
        self._esc(subject="legal threat")
        r = self.api.get(f"/api/escalations/unread_count/?organization={self.org.id}&brand={self.brand.id}")
        items = r.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(set(items[0].keys()), {"id", "sender", "sender_name", "subject"})
        self.assertEqual(items[0]["subject"], "legal threat")
        self.assertEqual(items[0]["sender"], "buyer@example.com")


class InternalEmailUnreadCountTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.user = User.objects.create_user("agent", password="pw")
        self.org.members.add(self.user)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def _ie(self, *, is_read=False, status=InternalEmail.STATUS_INTERNAL_REVIEW, subject="hr"):
        return InternalEmail.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            sender="boss@deodap.com", sender_name="The Boss", subject=subject,
            status=status, is_read=is_read)

    def _get(self):
        r = self.api.get(f"/api/internal-emails/unread_count/?organization={self.org.id}&brand={self.brand.id}")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_counts_only_unread_non_terminal(self):
        self._ie()                                              # unread -> counts
        self._ie()                                              # unread -> counts
        self._ie(is_read=True)                                  # read -> excluded
        self._ie(status=InternalEmail.STATUS_ARCHIVED)          # terminal -> excluded
        self._ie(status=InternalEmail.STATUS_DELETED)           # terminal -> excluded
        self.assertEqual(self._get()["count"], 2)

    def test_returns_items_for_toast(self):
        self._ie(subject="payroll")
        items = self._get()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["sender_name"], "The Boss")
        self.assertEqual(items[0]["subject"], "payroll")

    def test_opening_detail_marks_read_and_drops_count(self):
        ie = self._ie()
        self.assertEqual(self._get()["count"], 1)
        r = self.api.get(f"/api/internal-emails/{ie.id}/?organization={self.org.id}&brand={self.brand.id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._get()["count"], 0)               # badge clears after read
