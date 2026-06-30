"""
Offline tests for the order-ID duplicate detection (Ticket Logic Rule 1) and the
Gallabox sync orchestration (search -> update-or-create), both with fakes.

    python manage.py test apps.integrations.tests_gallabox apps.ingestion.tests_dedup
"""

from django.test import TestCase

from apps.brand_settings.models import BrandSettings
from apps.integrations import gallabox
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Message, Ticket


class FakeGallabox:
    def __init__(self, existing=None):
        self.existing = existing
        self.created = []
        self.updated = []
        self.messages = []

    def search_ticket(self, *, email=None, order_id=None, phone=None):
        return self.existing

    def create_ticket(self, payload):
        self.created.append(payload)
        return {"id": "gb-new-1"}

    def update_ticket(self, ticket_id, payload):
        self.updated.append((ticket_id, payload))
        return {"id": ticket_id}

    def add_message(self, ticket_id, text):
        self.messages.append((ticket_id, text))
        return {}


class GallaboxSyncTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="Where is my order?",
            extracted={"order_id": "DD9999", "issue_summary": "asking for tracking"},
        )

    def test_creates_when_none_exists(self):
        fake = FakeGallabox(existing=None)
        gid = gallabox.sync_ticket(self._ticket(), client=fake)
        self.assertEqual(gid, "gb-new-1")
        self.assertEqual(len(fake.created), 1)
        self.assertEqual(fake.created[0]["orderId"], "DD9999")

    def test_updates_when_open_ticket_exists(self):
        fake = FakeGallabox(existing={"id": "gb-7", "status": "open"})
        t = self._ticket()
        gid = gallabox.sync_ticket(t, client=fake)
        self.assertEqual(gid, "gb-7")
        self.assertEqual(len(fake.updated), 1)
        self.assertEqual(fake.messages[0][0], "gb-7")  # conversation appended
        t.refresh_from_db()
        self.assertEqual(t.extracted["gallabox_id"], "gb-7")

    def test_no_client_is_noop(self):
        self.assertIsNone(gallabox.sync_ticket(self._ticket(), client=None))

    def test_build_client_from_settings(self):
        BrandSettings.objects.create(brand=self.brand, integrations={
            "gallabox": {"api_key": "k", "api_secret": "s"}
        })
        self.brand.refresh_from_db()
        c = gallabox.build_client_for(self.brand)
        self.assertIsInstance(c, gallabox.GallaboxClient)
