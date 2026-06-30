"""
Dedicated INQUIRY workflow tests -- SINGLE-REPLY mode. Each field-collecting flow asks for
ALL details in one message and parses a single reply ("Name: X  Mobile: Y  City: Z"); the
inquiry/ticket is created the moment all required fields (and, for fraud, the screenshot) are
present. Company Profile / VIP / Other are immediate auto-replies. These flows NEVER enter the
support / order-verification flow.

    python manage.py test apps.ingestion.tests_inquiry
"""

from django.test import TestCase, override_settings

from apps.ingestion import inquiry, service
from apps.ingestion.tests_imap import FakeImap
from apps.ingestion.tests_smart import eml
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Inquiry, PendingConversation, Ticket


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class InquiryBase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _run(self, *emails):
        self.sent = []
        orig = service._send_customer_email
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body,
                              "attachments": k.get("attachments")}) or "<sent>")
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            service._send_customer_email = orig

    def _bodies(self):
        return "\n---\n".join(s["body"] for s in self.sent)

    def _reply(self, body, n, original="<a@x>", image=False):
        return eml(subject="Re: inquiry", body=body, message_id=f"<r{n}@x>",
                   in_reply_to=original, references=original, image=image)


class ParserTests(TestCase):
    def test_parse_single_reply_all_fields(self):
        d = inquiry.parse_fields("Name: Chintan Dabhi\nMobile: 7452638014\nCity: Rajkot",
                                 inquiry.flow_fields("DROPSHIPPING"))
        self.assertEqual(d, {"dropshipping_name": "Chintan Dabhi",
                             "dropshipping_mobile": "7452638014", "dropshipping_city": "Rajkot"})
        self.assertEqual(inquiry.missing_fields("DROPSHIPPING", d), [])

    def test_parse_tolerates_dash_and_case(self):
        d = inquiry.parse_fields("CITY - Rajkot\nInvestment = 60000\nmobile : 9876543210",
                                 inquiry.flow_fields("FRANCHISEE"))
        self.assertEqual(d["franchise_city"], "Rajkot")
        self.assertEqual(d["franchise_investment"], "60000")
        self.assertEqual(d["franchise_mobile"], "9876543210")

    def test_partial_reply_reports_missing(self):
        d = inquiry.parse_fields("Name: A\nCity: Rajkot", inquiry.flow_fields("FRANCHISEE"))
        self.assertEqual(set(inquiry.missing_fields("FRANCHISEE", d)),
                         {"franchise_investment", "franchise_mobile"})


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class SingleReplyInquiryTests(InquiryBase):
    # === Dropshipping (no YES gate; one reply) ========================================
    def test_dropshipping_single_reply(self):
        self._run(eml(subject="Dropshipping", body="I want dropshipping", message_id="<a@x>"),
                  self._reply("Name: Ravi Kumar\nMobile: 9876500000\nCity: Delhi", 1))
        self.assertEqual(Ticket.objects.count(), 0)
        inq = Inquiry.objects.get()
        self.assertEqual(inq.inquiry_type, "DROPSHIPPING")
        self.assertEqual(inq.data, {"dropshipping_name": "Ravi Kumar",
                                    "dropshipping_mobile": "9876500000",
                                    "dropshipping_city": "Delhi"})
        b = self._bodies()
        self.assertIn("Please reply with the following details", b)
        self.assertIn("• Full Name", b)            # asked for ALL fields at once
        self.assertIn("• City", b)
        self.assertNotIn("could not verify", b.lower())

    # === Franchisee ===================================================================
    def test_franchisee_single_reply(self):
        self._run(eml(subject="Franchise", body="I want a franchise", message_id="<a@x>"),
                  self._reply("City: Rajkot\nInvestment: 60000\nMobile: 9876543210", 1))
        self.assertEqual(Ticket.objects.count(), 0)
        inq = Inquiry.objects.get()
        self.assertEqual(inq.inquiry_type, "FRANCHISEE")
        self.assertEqual(inq.data["franchise_city"], "Rajkot")
        self.assertEqual(inq.data["franchise_mobile"], "9876543210")

    # === Invoice ======================================================================
    def test_invoice_single_reply_queued(self):
        self._run(eml(subject="Invoice", body="I need a GST invoice", message_id="<a@x>"),
                  self._reply("Name: Anil\nMobile: 9812345678\nOrder Number: 123456\n"
                              "GST Number: 24ABCDE1234F1Z5\nTrade Name: ABC Enterprise", 1))
        self.assertEqual(Ticket.objects.count(), 0)
        inq = Inquiry.objects.get()
        self.assertEqual(inq.inquiry_type, "INVOICE_REQUEST")
        self.assertEqual(inq.queue, "invoice_team")
        self.assertEqual(inq.data["invoice_order_number"], "123456")
        self.assertEqual(inq.data["invoice_gst_number"], "24ABCDE1234F1Z5")
        self.assertEqual(inq.data["invoice_trade_name"], "ABC Enterprise")

    # === incomplete reply -> ask only for the missing fields ==========================
    def test_incomplete_reply_reasks_only_missing(self):
        self._run(eml(subject="Dropshipping", body="dropshipping", message_id="<a@x>"),
                  self._reply("Name: Ravi", 1))         # missing mobile + city
        self.assertEqual(Inquiry.objects.count(), 0)
        b = self._bodies()
        self.assertIn("We still need", b)
        self.assertIn("Mobile", b)
        self.assertIn("City", b)
        # finishing reply with the rest completes it (resume in same conversation)
        self.mailbox.imap_last_uid = 0
        self.mailbox.save(update_fields=["imap_last_uid"])
        self._run(self._reply("Mobile: 9876500000\nCity: Delhi", 2))
        self.assertEqual(Inquiry.objects.count(), 1)

    # === Bulk menu -> Bulk Order single reply =========================================
    def test_bulk_order_single_reply(self):
        self._run(eml(subject="Bulk", body="wholesale bulk purchase", message_id="<a@x>"),
                  self._reply("1", 1),
                  self._reply("Name: Sunil\nMobile: 9800011122\nProduct: SKU123 / 500 Qty", 2))
        self.assertEqual(Ticket.objects.count(), 0)
        inq = Inquiry.objects.get()
        self.assertEqual(inq.inquiry_type, "BULK_ORDER")
        self.assertEqual(inq.data["bulk_product_details"], "SKU123 / 500 Qty")

    def test_vip_pricing_link_only(self):
        self._run(eml(subject="Bulk", body="wholesale inquiry", message_id="<a@x>"),
                  self._reply("2", 1))
        self.assertEqual(Inquiry.objects.get().inquiry_type, "VIP_BULK_PRICING")
        self.assertIn("https://deodap.in/pages/vip", self._bodies())


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in",
                   COMPANY_BROCHURE_PATH=__file__)        # any existing file stands in as PDF
class CompanyProfileAutoReplyTests(InquiryBase):
    """Company Profile -> immediate brochure auto-reply on the FIRST email. No ticket, no
    questions, status completed, COMPANY_PROFILE_SENT logged."""

    TRIGGERS = ["Company Profile", "Send Company Profile", "Business Details",
                "Company Information"]

    def test_all_triggers_auto_reply_with_pdf_no_ticket(self):
        for i, text in enumerate(self.TRIGGERS):
            with self.subTest(text=text):
                Inquiry.objects.all().delete()
                PendingConversation.objects.all().delete()
                self.mailbox.imap_last_uid = 0
                self.mailbox.save(update_fields=["imap_last_uid"])
                with self.assertLogs("apps.ingestion.service", level="INFO") as cm:
                    self._run(eml(subject=text, body=text, message_id=f"<c{i}@x>"))
                self.assertEqual(Ticket.objects.count(), 0)        # NO ticket
                inq = Inquiry.objects.get()
                self.assertEqual(inq.inquiry_type, "COMPANY_PROFILE")
                self.assertEqual(inq.status, "completed")
                last = self.sent[-1]
                self.assertEqual(last["subject"], "DeoDap Company Profile & Business Information")
                self.assertTrue(last["attachments"])               # company_profile.pdf attached
                self.assertEqual(last["attachments"][0][0], "company_profile.pdf")
                self.assertIn("COMPANY_PROFILE_SENT", "\n".join(cm.output))
                self.assertNotIn("Mobile", last["body"])           # never asks for details


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class FraudInquiryTests(InquiryBase):
    def _run_shop(self, *emails, shop=None):
        """Like _run but also injects a FakeShopify so the verified customer-name lookup runs."""
        from apps.integrations import context as ctx
        from apps.ingestion.tests_verification import FakeShopify
        self.sent = []
        clients = {"shopify": shop or FakeShopify(), "shipping": None, "gokwik": None}
        os, ob = service._send_customer_email, ctx.build_clients
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body}) or "<sent>")
        ctx.build_clients = lambda settings: clients
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            service._send_customer_email, ctx.build_clients = os, ob

    def _fraud_payment(self, screenshot=True, mid="a"):
        # report fraud -> menu -> "1" -> FRAUD_PAYMENT -> VERIFY (Shopify down -> proceed) ->
        # details. Uses _run (no Shopify configured -> cannot_verify -> proceeds with Unknown).
        self._run(
            eml(subject="Fraud", body="report fraud", message_id=f"<{mid}@x>"),
            self._reply("1", f"{mid}1", original=f"<{mid}@x>"),
            self._reply("my mobile 9550413577", f"{mid}v", original=f"<{mid}@x>"),   # STEP 1
            self._reply("Description: paid 5000 to fake agent\nName: Rahul\n"
                        "Fraudster Mobile: 9123456780\nAmount: 5000", f"{mid}2",
                        original=f"<{mid}@x>", image=screenshot))

    def test_fraud_payment_creates_high_ticket(self):
        self._fraud_payment()
        self.assertEqual(Ticket.objects.count(), 1)
        t = Ticket.objects.get()
        self.assertEqual(t.priority, Ticket.PRIORITY_HIGH)
        self.assertEqual(t.extracted["fraud_issue_type"], "FRAUD_PAYMENT")
        self.assertEqual(t.extracted["reporter_name"], "Rahul")       # typed name (kept aside)
        self.assertEqual(t.extracted["fraud_mobile"], "9123456780")
        self.assertEqual(t.extracted.get("payment_amount"), "5000")   # optional field captured
        self.assertTrue(t.extracted.get("payment_screenshot"))
        from apps.integrations.care_panel_store import _customer_name
        self.assertEqual(_customer_name(t), "Unknown")                # Shopify down -> Unknown
        b = self._bodies()
        self.assertIn("Payment Screenshot (Mandatory)", b)
        self.assertIn("Your complaint is registered.", b)             # STEP 4 wording
        self.assertIn("Ticket ID:", b)

    def test_fraud_payment_screenshot_mandatory(self):
        self._fraud_payment(screenshot=False)
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertIn("Screenshot", self._bodies())

    # --- ISSUE 1: skip the menu when the sub-category is already clear -----------------------
    def test_payment_fraud_skips_menu(self):
        # "I paid a fraud person" -> FRAUD_PAYMENT directly (no menu) -> STEP 1 asks to verify.
        self._run(eml(subject="Fraud", body="I paid a fraud person", message_id="<p@x>"))
        first = self.sent[0]["body"]
        self.assertNotIn("1. Payment Done to Fraudster", first)        # no menu
        self.assertIn("could not verify", first.lower())              # STEP 1 verify prompt

    def test_suspicious_call_skips_menu(self):
        for body in ("Get Suspicious Call", "Someone called asking OTP", "Fraud Call Received"):
            with self.subTest(body=body):
                Ticket.objects.all().delete()
                PendingConversation.objects.all().delete()
                self.mailbox.imap_last_uid = 0
                self.mailbox.save(update_fields=["imap_last_uid"])
                self._run(eml(subject="x", body=body, message_id="<s@x>"))
                self.assertNotIn("1. Payment Done to Fraudster", self.sent[0]["body"])

    def test_generic_fraud_shows_menu(self):
        self._run(eml(subject="x", body="I have a fraud issue", message_id="<g@x>"))
        first = self.sent[0]["body"]
        self.assertIn("1. Payment Done to Fraudster", first)
        self.assertIn("2. Get Suspicious Call", first)

    # --- STEP 1: auto-verify + never collect/ticket without verification --------------------
    def test_autoverify_from_first_email_then_collect(self):
        # First email already has the mobile -> verified directly -> STEP 2 details requested.
        from apps.ingestion.tests_verification import FakeShopify
        order = {"order_id": "262339239", "customer_name": "Divya", "customer_phone": "9550413577"}
        shop = FakeShopify(orders={"262339239": order}, by_phone={"9550413577": [order]})
        self._run_shop(
            eml(subject="Fraud", body="I paid a fraud person. Mobile: 9550413577",
                message_id="<a@x>"), shop=shop)
        self.assertIn("Payment Screenshot (Mandatory)", self.sent[-1]["body"])  # asked details
        self.assertNotIn("could not verify", self._bodies().lower())            # never re-asked

    def test_verification_fails_no_ticket(self):
        # Shopify reachable but NO order for the mobile -> 'could not verify', NO ticket.
        from apps.ingestion.tests_verification import FakeShopify
        self._run_shop(
            eml(subject="Fraud", body="I paid a fraud person. Mobile: 9550413577",
                message_id="<a@x>"),
            self._reply("Description: paid 5000\nName: x\nFraudster Mobile: 9123456780", 2,
                        image=True),
            shop=FakeShopify())                       # empty -> not_found
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertIn("could not verify", self._bodies().lower())

    def test_suspicious_call_no_screenshot_required(self):
        # CASE 2: screenshot optional -> ticket from text fields alone (after verification).
        self._run(
            eml(subject="x", body="suspicious call received", message_id="<a@x>"),
            self._reply("my mobile 9550413577", 1),                    # STEP 1 verify (down->ok)
            self._reply("Description: caller asked for OTP\nName: Rahul\n"
                        "Suspicious Mobile: 9111122233", 2))
        self.assertEqual(Ticket.objects.count(), 1)
        t = Ticket.objects.get()
        self.assertEqual(t.extracted["fraud_issue_type"], "FRAUD_ALERT")
        self.assertEqual(t.extracted["suspicious_mobile"], "9111122233")

    # --- ISSUE 2: customer name from VERIFIED lookup ----------------------------------------
    def test_customer_name_from_verified_mobile(self):
        from apps.ingestion.tests_verification import FakeShopify
        from apps.integrations.care_panel_store import _customer_name
        order = {"order_id": "262339239", "customer_name": "Divya", "customer_phone": "9550413577"}
        shop = FakeShopify(orders={"262339239": order}, by_phone={"9550413577": [order]})
        self._run_shop(
            eml(subject="Fraud", body="I paid a fraud person. Mobile: 9550413577",
                message_id="<a@x>"),
            self._reply("Description: paid 5000 to fake agent\nName: Sender Typed\n"
                        "Fraudster Mobile: 9123456780", 2, image=True),
            shop=shop)
        self.assertEqual(Ticket.objects.count(), 1)
        t = Ticket.objects.get()
        self.assertEqual(_customer_name(t), "Divya")                  # verified, NOT Unknown
        self.assertEqual(t.extracted.get("customer_name_source"), "shopify_verified")
        self.assertNotEqual(_customer_name(t), "Sender Typed")        # not the typed name

    def test_duplicate_fraud_ticket_detection(self):
        self._fraud_payment(mid="a")
        self.assertEqual(Ticket.objects.count(), 1)
        self.mailbox.imap_last_uid = 0
        self.mailbox.save(update_fields=["imap_last_uid"])
        self._fraud_payment(mid="b")
        self.assertEqual(Ticket.objects.count(), 1)          # no duplicate
        self.assertIn("You already have open ticket(s)", self._bodies())


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class FraudVerifiedNameAndDedupTests(InquiryBase):
    """Customer name from the verified order owner (Problem 1) + per-sub-category dedup
    (Problem 2)."""

    def _run_shop(self, *emails, shop=None):
        from apps.integrations import context as ctx
        from apps.ingestion.tests_verification import FakeShopify
        self.sent = []
        clients = {"shopify": shop or FakeShopify(), "shipping": None, "gokwik": None}
        os, ob = service._send_customer_email, ctx.build_clients
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body}) or "<sent>")
        ctx.build_clients = lambda settings: clients
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            service._send_customer_email, ctx.build_clients = os, ob

    def _shop(self, **by_phone):
        from apps.ingestion.tests_verification import FakeShopify
        mapping = {ph: [{"order_id": f"O{i}", "customer_name": nm, "customer_phone": ph}]
                   for i, (ph, nm) in enumerate(by_phone.items())}
        return FakeShopify(by_phone=mapping)

    def _name(self, t):
        from apps.integrations.care_panel_store import _customer_name
        return _customer_name(t)

    # Test 1 -- Payment Done to Fraudster, customer mobile -> order owner name.
    def test_payment_fraud_customer_name(self):
        self._run_shop(
            eml(subject="Fraud", body="Payment Done to Fraudster. Mobile: 9986498641",
                message_id="<a@x>"),
            self._reply("Description: paid 5000\nName: Typed Name\n"
                        "Fraudster Mobile: 9123456780", 2, image=True),
            shop=self._shop(**{"9986498641": "Divya"}))
        self.assertEqual(Ticket.objects.count(), 1)
        t = Ticket.objects.get()
        self.assertEqual(self._name(t), "Divya")             # NOT Unknown / typed / sender
        self.assertEqual(t.extracted.get("customer_name_source"), "shopify_verified")

    # Test 2 -- Get Suspicious Call, customer mobile -> order owner name.
    def test_suspicious_call_customer_name(self):
        self._run_shop(
            eml(subject="x", body="Get Suspicious Call. Mobile: 7601843922", message_id="<a@x>"),
            self._reply("Description: caller asked OTP\nName: Typed\n"
                        "Suspicious Mobile: 9123456780", 2),
            shop=self._shop(**{"7601843922": "Anita"}))
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(self._name(Ticket.objects.get()), "Anita")

    # Test 3 -- Payment ticket exists; a Suspicious Call must create a NEW (separate) ticket.
    def test_payment_and_call_are_separate_tickets(self):
        shop = self._shop(**{"9986498641": "Divya"})
        self._run_shop(
            eml(subject="Fraud", body="Payment Done to Fraudster. Mobile: 9986498641",
                message_id="<p@x>"),
            self._reply("Description: paid 5000\nName: x\nFraudster Mobile: 9123456780", "p2",
                        original="<p@x>", image=True),
            shop=shop)
        self.assertEqual(Ticket.objects.count(), 1)
        self.mailbox.imap_last_uid = 0
        self.mailbox.save(update_fields=["imap_last_uid"])
        self._run_shop(
            eml(subject="x", body="Get Suspicious Call. Mobile: 9986498641", message_id="<c@x>"),
            self._reply("Description: caller asked OTP\nName: x\nSuspicious Mobile: 9123456780",
                        "c2", original="<c@x>"),
            shop=shop)
        self.assertEqual(Ticket.objects.count(), 2)          # SEPARATE ticket, not reused
        types = sorted(t.extracted["fraud_issue_type"] for t in Ticket.objects.all())
        self.assertEqual(types, ["FRAUD_ALERT", "FRAUD_PAYMENT"])

    # Test 4 -- the lookup uses the CUSTOMER mobile, never the fraudster's.
    def test_lookup_uses_customer_mobile_not_fraudster(self):
        # Only the customer's number resolves an order; the fraudster's number does not.
        self._run_shop(
            eml(subject="Fraud", body="Payment Done to Fraudster. Mobile: 9986498641",
                message_id="<a@x>"),
            self._reply("Description: paid 5000\nName: x\nFraudster Mobile: 9123456780", 2,
                        image=True),
            shop=self._shop(**{"9986498641": "Divya"}))   # 9123456780 NOT in shop
        t = Ticket.objects.get()
        self.assertEqual(self._name(t), "Divya")             # resolved via customer mobile
        self.assertEqual(t.extracted.get("phone"), "9986498641")
        self.assertNotEqual(t.extracted.get("phone"), "9123456780")


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class FraudIncidentDedupTests(InquiryBase):
    """Dedup is per INCIDENT (sub-category + fraudster number). A fresh report of a DIFFERENT
    fraud must always create a new ticket -- never merge into an unrelated old one."""

    def _run_shop(self, *emails, shop=None):
        from apps.integrations import context as ctx
        from apps.ingestion.tests_verification import FakeShopify
        self.sent = []
        clients = {"shopify": shop or FakeShopify(), "shipping": None, "gokwik": None}
        os, ob = service._send_customer_email, ctx.build_clients
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body}) or "<sent>")
        ctx.build_clients = lambda settings: clients
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            service._send_customer_email, ctx.build_clients = os, ob

    def _report(self, mid, fraudster):
        from apps.ingestion.tests_verification import FakeShopify
        order = {"order_id": "O1", "customer_name": "Divya", "customer_phone": "9986498641"}
        shop = FakeShopify(by_phone={"9986498641": [order]})    # customer verifies
        self.mailbox.imap_last_uid = 0
        self.mailbox.save(update_fields=["imap_last_uid"])
        self._run_shop(
            eml(subject="Fraud", body="Payment Done to Fraudster. Mobile: 9986498641",
                message_id=f"<{mid}@x>"),
            self._reply(f"Description: paid 5000\nName: x\nFraudster Mobile: {fraudster}",
                        f"{mid}2", original=f"<{mid}@x>", image=True),
            shop=shop)

    def test_different_fraudster_creates_new_ticket(self):
        self._report("a", "9123456780")
        self.assertEqual(Ticket.objects.count(), 1)
        self._report("b", "9000000000")             # DIFFERENT fraudster
        self.assertEqual(Ticket.objects.count(), 2)  # -> NEW ticket, not merged

    def test_same_fraudster_dedups(self):
        self._report("a", "9123456780")
        self._report("b", "9123456780")             # SAME fraudster -> same incident
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertIn("You already have open ticket(s)", self._bodies())
