"""
Identifier detection during verification: a STANDALONE number is tried as an ORDER first and,
only when it is a 10-digit mobile, as a MOBILE; an email verifies as the registered email.

    python manage.py test apps.ingestion.tests_identifier
"""

from django.test import TestCase

from apps.ingestion import service
from apps.organizations.models import Brand, Mailbox, Organization


class FakeShopify:
    def __init__(self, orders=None, by_phone=None, by_email=None):
        self.orders = orders or {}
        self.by_phone = by_phone or {}
        self.by_email = by_email or {}
        self.calls = []

    def get_order(self, order_id):
        self.calls.append(("order", order_id))
        return self.orders.get(order_id)

    def recent_orders_by_phone(self, phone, limit=5):
        self.calls.append(("phone", phone))
        return self.by_phone.get(phone, [])

    def recent_orders_by_email(self, email, limit=5):
        self.calls.append(("email", email))
        return self.by_email.get(email, [])


def _order(order_id="262203508", name="Test Buyer"):
    return {"order_id": order_id, "customer_name": name, "customer_email": "buyer@shop.com",
            "shipped": True, "delivered": False, "raw_fulfillment_status": "In Transit"}


class VerificationIdentifierTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _msg(self, body, subject="Re: verify"):
        return {"message_id": "<v@x>", "gmail_message_id": "<v@x>", "from_email": "buyer@x.com",
                "to": "care@deodap.com", "subject": subject, "body_text": body, "body_html": "",
                "headers": {}, "attachments": [], "attachment_blobs": [], "in_reply_to": "",
                "references": []}

    def _verify(self, body, *, orders=None, by_phone=None, by_email=None):
        from apps.integrations import context as ctx
        shop = FakeShopify(orders=orders, by_phone=by_phone, by_email=by_email)
        ob = ctx.build_clients
        ctx.build_clients = lambda s: {"shopify": shop, "shipping": None, "gokwik": None}
        try:
            proceed, status, info, o, p, e = service._verify_reply_identifier(
                None, self.brand, self._msg(body))
        finally:
            ctx.build_clients = ob
        return proceed, status, info, (o, p, e), shop

    # --- extraction (order-first, mobile only if 10 digits) ----------------------------
    def test_extract_standalone_order(self):
        o, p, e = service._verification_identifiers(None, self._msg("262203508"))
        self.assertEqual((o, p, e), ("262203508", "", ""))       # 9-digit -> order only

    def test_extract_standalone_mobile(self):
        o, p, e = service._verification_identifiers(None, self._msg("8947261305"))
        self.assertEqual((o, p, e), ("8947261305", "8947261305", ""))  # 10-digit -> order + mobile

    def test_extract_standalone_email(self):
        o, p, e = service._verification_identifiers(None, self._msg("buyer@shop.com"))
        self.assertEqual((o, p, e), ("", "", "buyer@shop.com"))

    # --- verification outcomes ---------------------------------------------------------
    def test_standalone_order_number_verifies(self):
        proceed, status, info, ids, shop = self._verify(
            "262203508", orders={"262203508": _order("262203508")})
        self.assertTrue(proceed)
        self.assertEqual(status, "verified")
        self.assertEqual(info["matched_by"], "order_id")

    def test_standalone_mobile_number_verifies(self):
        proceed, status, info, ids, shop = self._verify(
            "8947261305", orders={}, by_phone={"8947261305": [_order("262203508")]})
        self.assertTrue(proceed)
        self.assertEqual(info["matched_by"], "mobile")
        self.assertIn(("order", "8947261305"), shop.calls)       # order tried FIRST
        self.assertIn(("phone", "8947261305"), shop.calls)       # then mobile

    def test_standalone_email_verifies(self):
        proceed, status, info, ids, shop = self._verify(
            "buyer@shop.com", by_email={"buyer@shop.com": [_order("262203508")]})
        self.assertTrue(proceed)
        self.assertEqual(info["matched_by"], "email")

    def test_invalid_number_fails(self):
        proceed, status, info, ids, shop = self._verify(
            "999999999", orders={}, by_phone={})                 # configured, no match
        self.assertFalse(proceed)
        self.assertEqual(status, "not_found")

    def test_order_priority_over_mobile(self):
        # A 10-digit number that IS an order -> matched as ORDER, mobile lookup never reached.
        proceed, status, info, ids, shop = self._verify(
            "8947261305", orders={"8947261305": _order("8947261305")},
            by_phone={"8947261305": [_order("999")]})
        self.assertTrue(proceed)
        self.assertEqual(info["matched_by"], "order_id")         # order wins
        self.assertEqual(info["order_id"], "8947261305")
        self.assertNotIn(("phone", "8947261305"), shop.calls)    # mobile lookup never tried

    def test_logs_emitted(self):
        with self.assertLogs("apps.ingestion.service", level="INFO") as cm:
            self._verify("262203508", orders={"262203508": _order("262203508")})
        blob = "\n".join(cm.output)
        for tag in ("IDENTIFIER_DETECTED", "TRY_ORDER_LOOKUP", "SHOPIFY_MATCH"):
            self.assertIn(tag, blob)
