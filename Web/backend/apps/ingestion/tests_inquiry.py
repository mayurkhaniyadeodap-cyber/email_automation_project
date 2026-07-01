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


class FraudParserTests(TestCase):
    """The fraud reply parser must extract Description + Fraudster Mobile from real replies:
    same-line OR next-line values, label variations, mixed case, markdown, bullets, currency
    symbols and Gmail-quoted replies -- and never re-ask for a field that was provided."""

    def _p(self, text):
        return inquiry.parse_fields(text, inquiry.flow_all_fields("FRAUD_PAYMENT"))

    def test_description_same_line(self):
        d = self._p("Description: Paid 5000 to fake employee\nFraudster Mobile: 7905597007")
        self.assertEqual(d["fraud_description"], "Paid 5000 to fake employee")
        self.assertEqual(d["fraud_mobile"], "7905597007")

    def test_description_next_line(self):
        d = self._p("Fraud Description:\nPaid 5000 to fake employee.\n\n"
                    "Fraudster Mobile:\n7905597007")
        self.assertEqual(d["fraud_description"], "Paid 5000 to fake employee.")
        self.assertEqual(d["fraud_mobile"], "7905597007")

    def test_exact_reported_customer_reply(self):
        # The exact reply from the bug report (₹ symbol, next-line values, 'Screenshot attached.').
        d = self._p("Fraud Description:\nPaid ₹5000 to fake DeoDap employee.\n\n"
                    "Fraudster Mobile:\n7905597007\n\nScreenshot attached.")
        self.assertEqual(d["fraud_description"], "Paid ₹5000 to fake DeoDap employee.")
        self.assertEqual(d["fraud_mobile"], "7905597007")
        self.assertEqual(inquiry.missing_fields("FRAUD_PAYMENT", d), [])   # nothing missing

    def test_description_and_brief_issue_labels(self):
        self.assertEqual(self._p("Fraud Description: a\nMobile: 1")["fraud_description"], "a")
        self.assertEqual(self._p("Description: b\nMobile: 1")["fraud_description"], "b")
        self.assertEqual(self._p("Brief Description: c\nMobile: 1")["fraud_description"], "c")
        self.assertEqual(self._p("Issue Description: d\nMobile: 1")["fraud_description"], "d")

    def test_all_mobile_label_variations(self):
        for lbl in ("Fraudster Mobile", "Fraudster Mobile Number", "Fraud Mobile", "Mobile",
                    "Mobile Number", "Fraudster Number", "Suspicious Mobile"):
            d = self._p(f"Description: x\n{lbl}: 7905597007")
            self.assertEqual(d.get("fraud_mobile"), "7905597007", f"label={lbl}")

    def test_mixed_case_markdown_and_bullets(self):
        d = self._p("**FRAUD DESCRIPTION:** Paid 5000\n- fraudster mobile : 7905597007")
        self.assertEqual(d["fraud_description"], "Paid 5000")
        self.assertEqual(d["fraud_mobile"], "7905597007")

    def test_gmail_quoted_reply(self):
        text = ("Fraud Description: paid 5000 to fake agent\nFraudster Mobile: 7905597007\n\n"
                "On Mon, Jul 1, 2026 at 3:00 PM DeoDap Support <care@deodap.com> wrote:\n"
                "> To help us investigate your payment fraud report, please reply with:\n"
                "> - Brief description of the fraud\n"
                "> - Fraudster's mobile number\n"
                "> - Payment screenshot (Mandatory)\n")
        d = self._p(text)
        self.assertEqual(d["fraud_description"], "paid 5000 to fake agent")
        self.assertEqual(d["fraud_mobile"], "7905597007")


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
        # Payment fraud -> ONE info-request email (no menu, no verify step) -> a single reply
        # with the details + the mandatory payment screenshot -> ticket. Uses _run (no Shopify).
        self._run(
            eml(subject="Fraud", body="Payment done to fraudster", message_id=f"<{mid}@x>"),
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
        self.assertNotIn("Please choose an option", b)                # NO option menu
        self.assertIn("Payment screenshot (Mandatory)", b)            # single info-request
        self.assertIn("Ticket ID:", b)

    def test_fraud_payment_screenshot_mandatory(self):
        self._fraud_payment(screenshot=False)
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertIn("Screenshot", self._bodies())

    def test_next_line_labels_with_screenshot_creates_ticket(self):
        # Reported bug: labels with the value on the NEXT line (+ ₹ + 'Screenshot attached.')
        # must extract Description + Fraudster Mobile and create the ticket -- never re-ask.
        self._run(
            eml(subject="Fraud", body="Payment done to fraudster", message_id="<n@x>"),
            self._reply("Fraud Description:\nPaid ₹5000 to fake DeoDap employee.\n\n"
                        "Fraudster Mobile:\n7905597007\n\nScreenshot attached.", "n2",
                        original="<n@x>", image=True))
        self.assertEqual(Ticket.objects.count(), 1)              # completed, not re-asked
        t = Ticket.objects.get()
        self.assertEqual(t.extracted["fraud_issue_type"], "FRAUD_PAYMENT")
        self.assertEqual(t.extracted["fraud_mobile"], "7905597007")
        self.assertIn("fake DeoDap employee", t.extracted.get("fraud_description", ""))
        self.assertIn("Ticket ID:", self._bodies())              # confirmation sent

    # --- ISSUE 1: fraud NEVER shows the option menu -- send the info-request straight away -----
    def test_payment_fraud_skips_menu(self):
        # "I paid a fraud person" -> FRAUD_PAYMENT directly: ONE info-request email, no menu.
        self._run(eml(subject="Fraud", body="I paid a fraud person", message_id="<p@x>"))
        first = self.sent[0]["body"]
        self.assertNotIn("1. Payment Done to Fraudster", first)        # no menu
        self.assertNotIn("Please choose an option", first)
        self.assertIn("payment fraud report", first.lower())          # the info-request itself
        self.assertIn("Payment screenshot (Mandatory)", first)

    def test_suspicious_call_skips_menu(self):
        for body in ("Get Suspicious Call", "Someone called asking OTP", "Fraud Call Received"):
            with self.subTest(body=body):
                Ticket.objects.all().delete()
                PendingConversation.objects.all().delete()
                self.mailbox.imap_last_uid = 0
                self.mailbox.save(update_fields=["imap_last_uid"])
                self._run(eml(subject="x", body=body, message_id="<s@x>"))
                self.assertNotIn("1. Payment Done to Fraudster", self.sent[0]["body"])

    def test_generic_fraud_no_menu_defaults_to_payment(self):
        # A generic fraud email (sub-type unclear) must NOT show a menu -- it defaults to the
        # Payment Fraud info-request so the customer gets one actionable email.
        self._run(eml(subject="x", body="I have a fraud issue", message_id="<g@x>"))
        first = self.sent[0]["body"]
        self.assertNotIn("1. Payment Done to Fraudster", first)
        self.assertNotIn("2. Get Suspicious Call", first)
        self.assertNotIn("Please choose an option", first)
        self.assertIn("Payment screenshot (Mandatory)", first)         # defaulted info-request

    # --- info-request goes out immediately; customer named from their own email identifier -----
    def test_info_request_sent_immediately_no_verify_step(self):
        # First email is clearly payment fraud -> ONE info-request email straight away (no verify
        # prompt, no menu). The mobile in that email is used later to name the ticket.
        from apps.ingestion.tests_verification import FakeShopify
        order = {"order_id": "262339239", "customer_name": "Divya", "customer_phone": "9550413577"}
        shop = FakeShopify(orders={"262339239": order}, by_phone={"9550413577": [order]})
        self._run_shop(
            eml(subject="Fraud", body="I paid a fraud person. Mobile: 9550413577",
                message_id="<a@x>"), shop=shop)
        self.assertIn("Payment screenshot (Mandatory)", self.sent[-1]["body"])  # asked details
        self.assertNotIn("could not verify", self._bodies().lower())            # no verify step
        self.assertNotIn("Please choose an option", self._bodies())

    def test_unverified_still_creates_ticket(self):
        # NEW: we no longer block on verification. Details received -> ticket is created (the
        # customer name resolves to Unknown when Shopify has no match); a human agent investigates.
        from apps.ingestion.tests_verification import FakeShopify
        from apps.integrations.care_panel_store import _customer_name
        self._run_shop(
            eml(subject="Fraud", body="I paid a fraud person", message_id="<a@x>"),
            self._reply("Description: paid 5000\nName: x\nFraudster Mobile: 9123456780", 2,
                        image=True),
            shop=FakeShopify())                       # empty -> no match
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(_customer_name(Ticket.objects.get()), "Unknown")
        self.assertNotIn("could not verify", self._bodies().lower())

    def test_suspicious_call_no_screenshot_required(self):
        # CASE 2: screenshot optional -> ticket from the text fields alone (no menu, no verify).
        self._run(
            eml(subject="x", body="suspicious call received", message_id="<a@x>"),
            self._reply("Registered Mobile: 9550413577\nRegistered Email: c@x.com\n"
                        "Suspicious Caller Mobile: 9111122233\n"
                        "Description: caller asked for OTP", 2))
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
class FraudEscalationRoutingTests(InquiryBase):
    """A reply that belongs to an ACTIVE fraud pending must be handled by the fraud workflow --
    NEVER hijacked by the High-Priority / escalation engine, even though such replies naturally
    contain escalation trigger words (police / cyber crime / legal action / consumer court)."""

    def test_payment_fraud_reply_with_escalation_words_creates_ticket(self):
        from apps.tickets.models import Escalation
        # Reply body contains 'cyber crime' + 'police complaint' -> would trip escalation. It must
        # instead create the fraud ticket because it belongs to the active fraud pending.
        self._run(
            eml(subject="Fraud", body="Payment done to fraudster", message_id="<f@x>"),
            self._reply("Description: paid 5000, this is a cyber crime, I will file a police "
                        "complaint\nFraudster Mobile: 9123456780", "f2",
                        original="<f@x>", image=True))
        self.assertEqual(Ticket.objects.count(), 1)                 # fraud ticket created
        self.assertEqual(Ticket.objects.get().extracted["fraud_issue_type"], "FRAUD_PAYMENT")
        self.assertEqual(Escalation.objects.count(), 0)             # NOT hijacked by escalation

    def test_suspicious_call_reply_with_escalation_words_creates_ticket(self):
        from apps.tickets.models import Escalation
        self._run(
            eml(subject="x", body="I got a suspicious call", message_id="<c@x>"),
            self._reply("Registered Mobile: 9550413577\nRegistered Email: c@x.com\n"
                        "Suspicious Caller Mobile: 9111122233\n"
                        "Description: the caller threatened legal action and a consumer court case",
                        "c2", original="<c@x>"))
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Ticket.objects.get().extracted["fraud_issue_type"], "FRAUD_ALERT")
        self.assertEqual(Escalation.objects.count(), 0)            # active fraud never escalates

    def test_high_priority_still_fires_for_new_complaint(self):
        from apps.tickets.models import Escalation
        # A NEW email (no active pending) with escalation words must STILL escalate.
        self._run(eml(subject="Legal", body="I will file a consumer court case and a police "
                      "complaint against you", message_id="<n@x>"))
        self.assertEqual(Escalation.objects.count(), 1)            # High-Priority still works
        self.assertEqual(Ticket.objects.count(), 0)

    def test_existing_escalation_reply_still_appends(self):
        from apps.tickets.models import Escalation
        # First email escalates; a threaded reply appends to the SAME escalation (no pending
        # matches an escalation, so the escalation path still runs) -- not duplicated, no ticket.
        self._run(
            eml(subject="Legal", body="consumer court case against you", message_id="<e1@x>"),
            eml(subject="Re: Legal", body="any update on my consumer court case?",
                message_id="<e2@x>", in_reply_to="<e1@x>", references="<e1@x>"))
        self.assertEqual(Escalation.objects.count(), 1)            # appended, not duplicated
        self.assertEqual(Ticket.objects.count(), 0)


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
            self._reply("Registered Mobile: 7601843922\nRegistered Email: c@x.com\n"
                        "Suspicious Caller Mobile: 9123456780\nDescription: caller asked OTP", 2),
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
            self._reply("Registered Mobile: 9986498641\nRegistered Email: c@x.com\n"
                        "Suspicious Caller Mobile: 9123456780\nDescription: caller asked OTP",
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
