"""
Tests for ship.deodap.com AWB verification (Mail Flow §5/§9).

    python manage.py test apps.integrations.tests_shipping
"""

from django.test import TestCase

from apps.integrations import shipping
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Ticket


class FakeShipping:
    def __init__(self, known):
        self.known = known          # {awb: tracking_dict}
        self.calls = []

    def track(self, awb):
        self.calls.append(awb)
        data = self.known.get(awb)
        return None if data is None else data


class AwbVerifyTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self, awb=None):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="where is my order",
            extracted={"awb": awb} if awb else {})

    def test_verify_returns_tracking_for_known_awb(self):
        clients = {"shipping": FakeShipping({"AWB123": {"status": "in_transit",
                   "tracking_url": "https://ship.deodap.com/t/AWB123", "edd": "2026-06-15"}})}
        out = shipping.verify_awb(self.brand, "AWB123", clients=clients)
        self.assertEqual(out["status"], "in_transit")

    def test_unknown_awb_returns_none(self):
        clients = {"shipping": FakeShipping({})}
        self.assertIsNone(shipping.verify_awb(self.brand, "NOPE", clients=clients))

    def test_no_shipping_client_skips_gracefully(self):
        self.assertIsNone(shipping.verify_awb(self.brand, "AWB1", clients={"shipping": None}))

    def test_annotate_marks_verified_and_fills_tracking(self):
        t = self._ticket(awb="AWB123")
        clients = {"shipping": FakeShipping({"AWB123": {"status": "out_for_delivery",
                   "tracking_url": "https://ship.deodap.com/t/AWB123", "edd": "2026-06-15"}})}
        shipping.annotate_awb_verification(t, clients=clients)
        t.refresh_from_db()
        self.assertTrue(t.extracted["awb_verified"])
        self.assertEqual(t.extracted["tracking_url"], "https://ship.deodap.com/t/AWB123")
        self.assertEqual(t.extracted["edd"], "2026-06-15")
        self.assertTrue(t.audit_log.filter(event="awb_verified").exists())

    def test_annotate_marks_unverified_for_unknown_awb(self):
        t = self._ticket(awb="FAKE999")
        clients = {"shipping": FakeShipping({})}
        shipping.annotate_awb_verification(t, clients=clients)
        t.refresh_from_db()
        self.assertFalse(t.extracted["awb_verified"])
        audit = t.audit_log.filter(event="awb_verified").last()
        self.assertFalse(audit.detail["verified"])

    def test_no_awb_is_noop(self):
        t = self._ticket()
        self.assertIsNone(shipping.annotate_awb_verification(t, clients={"shipping": FakeShipping({})}))
        self.assertFalse(t.audit_log.filter(event="awb_verified").exists())
