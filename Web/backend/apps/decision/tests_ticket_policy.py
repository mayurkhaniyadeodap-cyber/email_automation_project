"""
Auto-Reply vs Ticket policy (the customer's "do not create a ticket for every message"
spec): info / self-serve categories are auto-replied & closed (no Care Panel ticket, no
tracking link); action categories (payment, logistics, item issues, order/account
changes, fraud) always create a ticket.

    python manage.py test apps.decision.tests_ticket_policy
"""

from django.test import TestCase

from apps.brand_settings.models import BrandSettings
from apps.decision import engine, policy
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, Rule, SubTopic
from apps.tickets.models import Message, Ticket


class RequiresTicketTests(TestCase):
    """policy.requires_ticket -- the authoritative ticket-vs-auto-reply table."""

    def test_no_ticket_information_categories(self):
        # cat 5 (item/GST edits), 10 (offers), 11 (bulk), 14 (account help) auto-reply.
        for code in ("1", "5", "9", "10", "11", "12", "13", "14"):
            self.assertFalse(policy.requires_ticket(code), f"cat {code} should be auto-reply")

    def test_ticket_action_categories(self):
        # cat 15 (app/website faults) creates a ticket now.
        for code in ("2", "3", "4", "6", "7", "8", "15", "16"):
            self.assertTrue(policy.requires_ticket(code), f"cat {code} should be a ticket")

    def test_taxonomy_sub_topic_routing(self):
        # Delivery: tracking auto-replies; logistics actions create tickets.
        self.assertFalse(policy.requires_ticket("1", "Shipment Tracking", "track my order"))
        for s in ("Delayed Delivery", "Undelivered Issue", "Out For Delivery Issue",
                  "Reschedule Delivery", "Call Delivery Agent", "Delivery Time Info"):
            self.assertTrue(policy.requires_ticket("1", s, ""), s)
        # Make changes: address/phone -> ticket; item/GST edits -> auto.
        self.assertTrue(policy.requires_ticket("2", "Update Address / Phone", ""))
        self.assertFalse(policy.requires_ticket("5", "Add / Update Items", ""))
        self.assertFalse(policy.requires_ticket("5", "Add / Update GST Details", ""))
        # Website/App: faults -> ticket; delete-account/data-privacy -> auto.
        for s in ("App Crashing / Not Loading", "Cart Not Saving Items",
                  "Checkout Page Not Load", "OTP / Notifications Not Received"):
            self.assertTrue(policy.requires_ticket("15", s, ""), s)
        self.assertFalse(policy.requires_ticket("14", "Delete Account", ""))
        self.assertFalse(policy.requires_ticket("14", "Data & Privacy Security", ""))

    def test_fraud_always_ticket(self):
        self.assertTrue(policy.requires_ticket("16", "Report Fraud", "got a suspicious call"))

    def test_payment_problem_is_ticket_but_invoice_copy_is_info(self):
        self.assertTrue(policy.requires_ticket("8", "Payment", "I was charged twice"))
        self.assertTrue(policy.requires_ticket("8", "Refund", "payment failed, money debited"))
        self.assertFalse(policy.requires_ticket("8", "Invoice", "please send invoice copy for my order"))
        self.assertFalse(policy.requires_ticket("8", "Invoice", "I need the GST invoice"))

    def test_invoice_words_with_a_payment_problem_still_ticket(self):
        # "invoice" present but it's really a payment dispute -> ticket.
        self.assertTrue(policy.requires_ticket("8", "Invoice", "invoice copy shows I was overcharged"))

    def test_bulk_and_inquiry_are_auto_reply(self):
        # Bulk Order Inquiry / VIP Bulk Pricing / seller / dropship are all INQUIRY auto-replies
        # now (handled by the dedicated Inquiry workflow), never a support ticket.
        self.assertFalse(policy.requires_ticket("11", "Bulk Order Inquiry", "bulk inquiry"))
        self.assertFalse(policy.requires_ticket("11", "VIP Bulk Pricing", "vip pricing"))
        self.assertFalse(policy.requires_ticket("11", "Seller", "how do I become a seller?"))
        self.assertFalse(policy.requires_ticket("11", "Dropship", "dropshipping requirements?"))

    def test_account_change_is_ticket_help_is_info(self):
        self.assertTrue(policy.requires_ticket("14", "Account", "please update my phone number"))
        self.assertTrue(policy.requires_ticket("14", "Account", "did not receive otp"))
        # Delete account / data privacy are self-serve auto-replies now (customer taxonomy).
        self.assertFalse(policy.requires_ticket("14", "Account", "I want to delete account"))
        self.assertFalse(policy.requires_ticket("14", "Account", "data privacy concern"))
        self.assertFalse(policy.requires_ticket("14", "Account", "how do I reset my password?"))
        self.assertFalse(policy.requires_ticket("14", "Account", "where is my order history"))

    def test_unknown_category_defaults_to_ticket(self):
        self.assertTrue(policy.requires_ticket("", "", "weird message"))
        self.assertTrue(policy.requires_ticket("99", "", ""))


class EngineTicketPolicyTests(TestCase):
    """End-to-end: the guardrail in decide() turns the policy into Route A (no ticket)
    or a ticketed route."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, confidence_threshold=0.75)

    def _sub(self, cat_code, cat_name, code, name, action=Rule.ACTION_INFO_ONLY,
             then="Here is the information you asked for."):
        cat, _ = Category.objects.get_or_create(
            brand=self.brand, code=cat_code, defaults={"name": cat_name})
        sub = SubTopic.objects.create(category=cat, code=code, name=name)
        Rule.objects.create(sub_topic=sub, condition="Always", then_response=then, action=action)
        return sub

    def _ticket(self, sub, subject="hi"):
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject=subject, sub_topic_ref=sub,
            category_ref=sub.category, status=Ticket.STATUS_CLASSIFIED,
            classification_status=Ticket.CLS_CLASSIFIED, ai_confidence=0.95)
        Message.objects.create(ticket=t, direction=Message.DIRECTION_INBOUND,
                               from_email="b@x.com", subject=subject, body_text="?")
        return t

    def test_info_category_auto_replies_no_ticket(self):
        # Cat 13 (Store Info) -> auto-reply & close (Route A): ai_handled + AUTO_RESOLVED.
        sub = self._sub("13", "Store Info", "13.1", "Store hours")
        plan = engine.decide(self._ticket(sub))
        self.assertEqual(plan.send_mode, engine.AUTO)
        self.assertEqual(plan.status, Ticket.STATUS_AUTO_RESOLVED)
        self.assertTrue(plan.ai_handled)

    def test_action_category_forces_ticket_even_with_info_rule(self):
        # Cat 3 (Delivery Issues) with an info_only rule must NOT auto-close -> ticket.
        sub = self._sub("3", "Delivery Issues", "3.9", "Other", then="Noted.")
        plan = engine.decide(self._ticket(sub))
        self.assertNotEqual(plan.status, Ticket.STATUS_AUTO_RESOLVED)
        self.assertFalse(plan.ai_handled)
        self.assertIn("policy_requires_ticket", plan.reasons)

    def test_account_change_forces_ticket(self):
        # Cat 14 sub-topic that is an account CHANGE -> ticket (not auto-reply).
        sub = self._sub("14", "Account", "14.1", "Update contact",
                        then="We can help with that.")
        plan = engine.decide(self._ticket(sub, subject="please update my email address"))
        self.assertNotEqual(plan.status, Ticket.STATUS_AUTO_RESOLVED)  # -> a ticket
        self.assertNotEqual(plan.send_mode, engine.AUTO)               # never auto-closed

    def test_account_help_auto_replies(self):
        # Cat 14 self-serve help (password reset) -> auto-reply & close.
        sub = self._sub("14", "Account", "14.2", "Password help",
                        then="To reset your password, open Account > Reset.")
        plan = engine.decide(self._ticket(sub, subject="how do I reset my password?"))
        self.assertEqual(plan.status, Ticket.STATUS_AUTO_RESOLVED)
        self.assertTrue(plan.ai_handled)

    def test_no_rule_info_category_auto_replies_via_responder(self):
        from apps.classifier import service as classifier

        class FakeProvider:
            def generate_text(self, system, user):
                return "Our store is open 10am to 8pm, Monday to Saturday."

        cat, _ = Category.objects.get_or_create(
            brand=self.brand, code="13", defaults={"name": "Store Info"})
        sub = SubTopic.objects.create(category=cat, code="13.2", name="Hours")  # NO rule
        t = self._ticket(sub, subject="what are your store hours?")
        orig = classifier.build_provider
        classifier.build_provider = lambda s: FakeProvider()
        try:
            engine.run(t)
        finally:
            classifier.build_provider = orig
        t.refresh_from_db()
        self.assertEqual(t.status, Ticket.STATUS_AUTO_RESOLVED)        # no ticket
        out = t.messages.filter(direction=Message.DIRECTION_OUTBOUND).first()
        self.assertIsNotNone(out)
        self.assertIn("store", out.body_text.lower())

    def test_info_category_with_no_answer_falls_back_to_agent(self):
        # NO_TICKET category but we genuinely can't answer (no rule, no AI key) ->
        # must NOT silently close: downgrade to an agent so a ticket gets created.
        cat, _ = Category.objects.get_or_create(
            brand=self.brand, code="13", defaults={"name": "Store Info"})
        sub = SubTopic.objects.create(category=cat, code="13.2", name="Misc")  # NO rule
        t = self._ticket(sub, subject="random question")
        plan = engine.run(t)                                           # no provider configured
        t.refresh_from_db()
        self.assertNotEqual(t.status, Ticket.STATUS_AUTO_RESOLVED)
        self.assertIn("no_auto_answer", plan.reasons)
