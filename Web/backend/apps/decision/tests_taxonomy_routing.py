"""
Authoritative ticket-vs-auto-reply routing for the FULL customer taxonomy. Support
categories are resolved by policy.route_category(); the five Inquiry categories + Bulk
Purchase are handled by the dedicated Inquiry workflow (auto-reply, never a support ticket).

    python manage.py test apps.decision.tests_taxonomy_routing
"""

from django.test import TestCase

from apps.decision import policy
from apps.ingestion import inquiry


class TaxonomyRoutingTests(TestCase):
    # (category_code, sub_topic) -> must CREATE a ticket
    TICKET = [
        ("8", "Payment Issue"),
        # Delivery related
        ("1", "Delayed Delivery"), ("1", "Undelivered Issue"), ("1", "Out For Delivery Issue"),
        ("4", "Cancelled Delivery (RTO)"), ("1", "Urgent Request"), ("1", "Delivery Time Info"),
        ("1", "Call Delivery Agent"), ("1", "Reschedule Delivery"), ("7", "Check Refund Status"),
        # Delivered item related
        ("3", "Damaged Item"), ("3", "Defective Item"), ("3", "Quality Issue"),
        ("3", "Missing Item"), ("3", "Wrong Item"), ("3", "Quantity Issue"), ("3", "Other Issue"),
        # Make changes to order
        ("2", "Update Address / Phone"),
        # Website / app related (faults + contact change / OTP)
        ("15", "App Crashing / Not Loading"), ("15", "Cart Not Saving Items"),
        ("15", "Checkout Page Not Load"), ("14", "Update Phone / Email"),
        ("14", "OTP / Notifications Not Received"),
        # Report fraud (HIGH)
        ("16", "Payment Done To Fraudster"), ("16", "Get Suspicious Call"),
    ]

    # (category_code, sub_topic) -> AUTO-REPLY (no ticket) -- support side (verified first)
    AUTO_REPLY = [
        ("1", "Shipment Tracking"),
        ("5", "Add / Update Items"), ("5", "Add / Update GST Details"),
        ("10", "All Offer Queries"),
        ("14", "Delete Account"), ("14", "Data & Privacy Security"),
    ]

    def test_ticket_categories_route_to_ticket(self):
        for code, sub in self.TICKET:
            self.assertEqual(policy.route_category(code, sub), policy.ROUTE_TICKET,
                             f"{code} / {sub} should CREATE a ticket")
            self.assertTrue(policy.requires_ticket(code, sub))

    def test_auto_reply_categories_route_to_auto(self):
        for code, sub in self.AUTO_REPLY:
            self.assertEqual(policy.route_category(code, sub), policy.ROUTE_AUTO_REPLY,
                             f"{code} / {sub} should AUTO-REPLY (no ticket)")
            self.assertFalse(policy.requires_ticket(code, sub))

    def test_inquiry_categories_handled_by_inquiry_workflow(self):
        # These never reach the support policy -- the Inquiry workflow intercepts them and
        # auto-replies / collects details (NO support ticket).
        cases = {
            "I want a franchise": inquiry.FRANCHISEE,
            "interested in dropshipping": inquiry.DROPSHIPPING,
            "please send your company profile": inquiry.COMPANY_PROFILE,
            "i need a gst invoice": inquiry.INVOICE_REQUEST,
            "general inquiry / other inquiry": inquiry.OTHER_INQUIRY,
            "bulk order inquiry wholesale": inquiry.BULK_PURCHASE,
            "vip bulk pricing wholesale": inquiry.BULK_PURCHASE,
        }
        for text, expected in cases.items():
            self.assertEqual(inquiry.detect_inquiry_type(text), expected, text)

    def test_tracking_vs_logistics_split(self):
        # Same delivery category, opposite routing -> proves sub-topic-level resolution.
        self.assertEqual(policy.route_category("1", "Shipment Tracking", "where is my order"),
                         policy.ROUTE_AUTO_REPLY)
        self.assertEqual(policy.route_category("1", "Reschedule Delivery", ""),
                         policy.ROUTE_TICKET)

    def test_account_split(self):
        self.assertEqual(policy.route_category("14", "Delete Account", ""),
                         policy.ROUTE_AUTO_REPLY)
        self.assertEqual(policy.route_category("14", "Update Phone / Email", ""),
                         policy.ROUTE_TICKET)
