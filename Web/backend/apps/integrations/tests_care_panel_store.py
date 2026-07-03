"""
Tests for the Care Panel store-json integration: the field-agnostic tracking
extractor, storing on the ticket, and the tracking-style confirmation email.

    python manage.py test apps.integrations.tests_care_panel_store
"""

from django.test import TestCase, override_settings

from apps.integrations import care_panel_store
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Message, Ticket


class ExtractTrackingTests(TestCase):
    def test_explicit_tracking_url_field(self):
        url, num = care_panel_store.extract_tracking(
            {"ticket_number": "2606090601", "tracking_url": "https://care.deodap.in/t?id=gKp64KxaAz"})
        self.assertEqual(url, "https://care.deodap.in/t?id=gKp64KxaAz")
        self.assertEqual(num, "2606090601")

    def test_alternate_field_names(self):
        url, num = care_panel_store.extract_tracking(
            {"data": {"ticket_no": "999", "public_url": "https://care.deodap.in/t?id=AbC123"}})
        self.assertEqual(url, "https://care.deodap.in/t?id=AbC123")
        self.assertEqual(num, "999")

    def test_regex_fallback_when_no_named_field(self):
        # URL only appears inside a 'message' string -> regex still finds it.
        url, num = care_panel_store.extract_tracking(
            {"success": True, "message": "View at https://care.deodap.in/t?id=Zz9 now",
             "id": 42})
        self.assertEqual(url, "https://care.deodap.in/t?id=Zz9")
        self.assertEqual(num, "42")

    def test_builds_url_from_hash(self):
        # The REAL store-json success response: data.hash (no full URL).
        url, num = care_panel_store.extract_tracking({
            "success": "success", "message": "ticket created successfully.",
            "data": {"hash": "EVPxcdvbvP4", "ticket_number": "2606110086"}})
        self.assertEqual(url, "https://care.deodap.in/t?id=EVPxcdvbvP4")
        self.assertEqual(num, "2606110086")

    def test_nothing_found(self):
        self.assertEqual(care_panel_store.extract_tracking({"ok": True}), ("", ""))


class StoreTicketTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="missing product",
            extracted={"order_id": "262203508", "phone": "9582872335"})

    def test_store_saves_tracking_and_number(self):
        class FakeClient:
            def store(self, payload):
                return 200, {"ticket_number": "2606090601",
                             "tracking_url": "https://care.deodap.in/t?id=gKp64KxaAz"}, "{}"
        t = self._ticket()
        url = care_panel_store.store_ticket(t, client=FakeClient())
        self.assertEqual(url, "https://care.deodap.in/t?id=gKp64KxaAz")
        t.refresh_from_db()
        self.assertEqual(t.tracking_url, "https://care.deodap.in/t?id=gKp64KxaAz")
        self.assertEqual(t.ticket_number, "2606090601")
        self.assertTrue(t.audit_log.filter(event="care_panel_stored").exists())

    def test_store_saves_hash_from_real_response(self):
        # The REAL store-json success shape: data.hash -> care_panel_ticket_id + URL.
        class FakeClient:
            def store(self, payload):
                return 200, {"success": "success", "message": "ticket created successfully.",
                             "data": {"hash": "EVPxcdvbvP4", "ticket_number": "2606110086"}}, "{}"
        t = self._ticket()
        url = care_panel_store.store_ticket(t, client=FakeClient())
        self.assertEqual(url, "https://care.deodap.in/t?id=EVPxcdvbvP4")
        t.refresh_from_db()
        self.assertEqual(t.tracking_url, "https://care.deodap.in/t?id=EVPxcdvbvP4")
        self.assertEqual(t.ticket_number, "2606110086")
        self.assertEqual(t.extracted["care_panel_ticket_id"], "EVPxcdvbvP4")  # hash saved

    def test_store_failure_audited(self):
        class FakeClient:
            def store(self, payload):
                return 401, None, "Unauthenticated."
        t = self._ticket()
        self.assertEqual(care_panel_store.store_ticket(t, client=FakeClient()), "")
        self.assertTrue(t.audit_log.filter(event="care_panel_store_failed").exists())

    def test_http200_but_success_failed_is_treated_as_failure(self):
        class FakeClient:
            def store(self, payload):
                return 200, {"success": "failed", "message": "Something went wrong.",
                             "data": []}, '{"success":"failed"}'
        t = self._ticket()
        self.assertEqual(care_panel_store.store_ticket(t, client=FakeClient()), "")
        t.refresh_from_db()
        self.assertEqual(t.tracking_url, "")
        self.assertTrue(t.audit_log.filter(event="care_panel_store_failed").exists())

    def test_no_phone_fails_fast_without_calling_api(self):
        # store-json is phone-keyed; without a phone we must NOT call the API (it would
        # 400) -- fail fast with a clear no_phone reason. (Root cause of the missing link.)
        calls = []

        class FakeClient:
            def store(self, payload):
                calls.append(payload)
                return 200, {"tracking_url": "https://care.deodap.in/t?id=X"}, "{}"

        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="missing product",
            extracted={"order_id": "262203508"})       # NO phone
        self.assertEqual(care_panel_store.store_ticket(t, client=FakeClient()), "")
        self.assertEqual(calls, [])                      # API never called
        t.refresh_from_db()
        self.assertEqual(t.tracking_url, "")
        failed = t.audit_log.filter(event="care_panel_store_failed").last()
        self.assertEqual(failed.detail.get("reason"), "no_phone")


class StoreRetryTests(TestCase):
    """store-json sometimes answers with a transient 'Something went wrong, Please try
    again later.' -- the original cause of a ticket stuck on an internal fallback link.
    A transient error must be RETRIED; a real validation error must NOT."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="missing product",
            extracted={"order_id": "262203508", "phone": "9582872335"})

    def test_transient_then_success_is_retried(self):
        # Fail transiently twice, then succeed -> store_ticket recovers (no manual re-run).
        class FlakyClient:
            def __init__(self):
                self.calls = 0

            def store(self, payload):
                self.calls += 1
                if self.calls < 3:
                    return 200, {"success": "failed",
                                 "message": "Something went wrong, Please try again later.",
                                 "data": []}, '{"success":"failed"}'
                return 200, {"success": "success", "message": "ticket created successfully.",
                             "data": {"hash": "RtRyOK123", "ticket_number": "2606160999"}}, "{}"
        client = FlakyClient()
        t = self._ticket()
        url = care_panel_store.store_ticket(t, client=client)
        self.assertEqual(client.calls, 3)                       # retried until success
        self.assertEqual(url, "https://care.deodap.in/t?id=RtRyOK123")
        t.refresh_from_db()
        self.assertEqual(t.extracted["care_panel_ticket_id"], "RtRyOK123")

    def test_validation_error_is_not_retried(self):
        # A real 4xx validation error will never succeed -> fail fast, do NOT retry.
        class StrictClient:
            def __init__(self):
                self.calls = 0

            def store(self, payload):
                self.calls += 1
                return 422, {"success": "failed", "message": "The phone field is required."}, \
                    '{"success":"failed"}'
        client = StrictClient()
        t = self._ticket()
        self.assertEqual(care_panel_store.store_ticket(t, client=client), "")
        self.assertEqual(client.calls, 1)                       # no retry on validation error

    def test_transient_exhausts_attempts_then_audits(self):
        class AlwaysFlaky:
            def __init__(self):
                self.calls = 0

            def store(self, payload):
                self.calls += 1
                return 503, None, "Service Unavailable"
        client = AlwaysFlaky()
        t = self._ticket()
        self.assertEqual(care_panel_store.store_ticket(t, client=client), "")
        self.assertEqual(client.calls, care_panel_store.STORE_MAX_ATTEMPTS)
        failed = t.audit_log.filter(event="care_panel_store_failed").last()
        self.assertTrue(failed.detail.get("transient"))
        self.assertEqual(failed.detail.get("attempts"), care_panel_store.STORE_MAX_ATTEMPTS)


class DeliveredItemIssueMapTests(TestCase):
    """The reported BUG: a delivered-item complaint must map to its SPECIFIC real Care Panel
    issue (Damaged->8, Defective->16, Missing->7, Wrong->9, Quantity->10, Quality->18),
    NEVER to id 3 'Order Shown Delivered But Not Received'."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self, **kw):
        defaults = dict(organization=self.org, brand=self.brand, mailbox=self.mailbox,
                        customer_email="b@x.com")
        defaults.update(kw)
        return Ticket.objects.create(**defaults)

    def test_damaged_maps_to_8_never_not_received(self):
        # "My order is damaged" mis-classified by the AI as a not-received sub-topic.
        t = self._ticket(category="3. Delivery Issues (Post-Delivery)",
                         sub_topic="Order Shown Delivered But Not Received",
                         issue_summary="The customer's order arrived damaged.",
                         subject="My order is damaged")
        issue_id, issue_name, source = care_panel_store.resolve_issue(t)
        self.assertEqual(issue_id, "8")
        self.assertEqual(issue_name, "Damaged item")
        self.assertNotEqual(issue_id, "3")
        self.assertNotIn("not received", issue_name.lower())

    def test_delivered_but_not_received_maps_to_issue_3(self):
        # The reported bug: "delivered but not received" must map to id 3, NOT Missing (7).
        t = self._ticket(category="3. Delivery Issues (Post-Delivery)",
                         sub_topic="Order Shown Delivered But Not Received",
                         issue_summary="Tracking shows delivered but I have not received the package.",
                         subject="Tracking shows delivered but not received")
        issue_id, issue_name, source = care_panel_store.resolve_issue(t)
        self.assertEqual(issue_id, "3")
        self.assertEqual(issue_name, "order not received but shown as Delivered")
        self.assertNotEqual(issue_id, "7")              # NOT Missing Items

    def test_each_delivered_item_subtype_maps_to_its_issue(self):
        cases = {
            "the item is damaged / broken": ("8", "Damaged item"),
            "product is defective, not working": ("16", "defective / not working item"),
            "one item is missing from my order": ("7", "missing item"),
            "received the wrong item": ("9", "wrong item"),
            "quantity issue, received less quantity": ("10", "item qty. issue"),
            "bad quality product received": ("18", "item quality issue"),
        }
        for summary, (eid, ename) in cases.items():
            t = self._ticket(category="3. Delivery Issues (Post-Delivery)",
                             issue_summary=summary, subject="complaint")
            issue_id, issue_name, _ = care_panel_store.resolve_issue(t)
            self.assertEqual(issue_id, eid, summary)
            self.assertEqual(issue_name, ename, summary)

    def test_cancellation_maps_to_cancel_order_5(self):
        t = self._ticket(issue_summary="please cancel my order", subject="cancel order")
        self.assertEqual(care_panel_store.resolve_issue(t)[0], "5")

    def test_unknown_falls_back_to_default_19(self):
        # Default catch-all is now Gallabox 19 "multiple issues" (a VALID id; old 6 didn't exist).
        t = self._ticket(category="", subject="hello", issue_summary="random")
        self.assertEqual(care_panel_store.resolve_issue(t)[0], "19")

    # --- PAYMENT-deducted-but-no-order -> id 12 ("CyberFraud Report"), NEVER an Other default -----
    # (There is no valid id 6 in the real Care Panel catalog -> the brand files these as CyberFraud.)
    def test_payment_deducted_no_order_maps_to_12(self):
        # The reported bug: "Payment deducted but order not placed" showed "Other Delivery Related
        # Issue" instead of CyberFraud Report (12).
        t = self._ticket(category="8. Payment & Invoice",
                         sub_topic="Payment Deducted But Order Not Placed",
                         issue_summary="Payment of 599 deducted but the order was not placed",
                         subject="Payment deducted but order not placed")
        issue_id, issue_name, source = care_panel_store.resolve_issue(t)
        self.assertEqual(issue_id, "12")
        self.assertEqual(issue_name, "CyberFraud Report")
        self.assertNotIn("delivery", issue_name.lower())

    def test_payment_detected_even_if_ai_miscategorised(self):
        # AI mislabelled the category, but the text clearly says payment deducted, no order -> 12.
        t = self._ticket(category="3. Delivery Issues", sub_topic="",
                         issue_summary="amount debited but order not placed",
                         subject="money deducted no order")
        self.assertEqual(care_panel_store.resolve_issue(t)[0], "12")

    def test_payment_detected_from_message_body_when_ai_summary_generic(self):
        # The reported production case: AI category + issue_summary are GENERIC ("Other Delivery
        # Related Issue") but the customer's ORIGINAL message clearly states a payment problem.
        # Detection must read the inbound message body -> issue 12, not an Other/Delivery default.
        t = self._ticket(category="3. Delivery Issues", sub_topic="",
                         issue_summary="Other Delivery Related Issue", subject="order issue")
        Message.objects.create(
            ticket=t, direction=Message.DIRECTION_INBOUND, from_email="b@x.com",
            subject="Payment deducted but order not placed",
            body_text="Hi, Rs 599 has been deducted from my account but my order was not "
                      "placed. Mobile Number: 7349498204")
        issue_id, issue_name, _ = care_panel_store.resolve_issue(t)
        self.assertEqual(issue_id, "12")
        self.assertEqual(issue_name, "CyberFraud Report")

    def test_fraud_maps_to_cyberfraud_12(self):
        t = self._ticket(issue_summary="this is a scam, I was defrauded by a fake call",
                         subject="report fraud")
        issue_id, issue_name, _ = care_panel_store.resolve_issue(t)
        self.assertEqual(issue_id, "12")
        self.assertEqual(issue_name, "CyberFraud Report")

    # --- ACCOUNT group -> id 20 ("Account Related issues"), NOT Website/App (21) ----------------
    def test_otp_not_received_maps_to_account_20(self):
        # The reported bug: OTP / Notifications belongs to Account (20), not Website/App (21).
        t = self._ticket(category="15. Website / App Related",
                         sub_topic="OTP / Notifications Not Received",
                         issue_summary="Customer is not receiving OTP", subject="otp not received")
        issue_id, issue_name, source = care_panel_store.resolve_issue(t)
        self.assertEqual(issue_id, "20")
        self.assertEqual(issue_name, "Account Related issues")
        self.assertNotEqual(issue_id, "21")

    def test_otp_detected_from_text_alone(self):
        t = self._ticket(category="", sub_topic="", subject="otp not received",
                         issue_summary="hii I did not get the otp")
        self.assertEqual(care_panel_store.resolve_issue(t)[0], "20")

    def test_each_account_subtopic_maps_to_20(self):
        for sub in ("Password Reset Error", "Update Phone / Email", "Delete Account",
                    "Data & Privacy Security", "OTP / Notifications Not Received",
                    "View Order History", "Create New Account", "Manage Saved Addresses"):
            t = self._ticket(sub_topic=sub, subject="account help")
            self.assertEqual(care_panel_store.resolve_issue(t)[0], "20", sub)

    def test_website_app_still_maps_to_21(self):
        # Genuine Website/App sub-topics must STILL map to 21 (no regression).
        t = self._ticket(category="15. Website / App Related",
                         sub_topic="Checkout Page Not Load", subject="checkout fails")
        self.assertEqual(care_panel_store.resolve_issue(t)[0], "21")


class ReStoreInternalTrackingTests(TestCase):
    """A ticket left on an INTERNAL fallback link (store-json failed earlier) must be
    re-attempted; one that already has a REAL Care Panel hash must NOT be re-stored."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def test_internal_tracking_ticket_is_restored(self):
        from apps.ingestion import service
        from apps.integrations import care_panel_store as cps

        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="cancel my order",
            tracking_url="http://192.168.1.2:8000/t?id=7a07e25493",
            extracted={"order_id": "262324646", "phone": "9414510548",
                       "internal_tracking": True, "tracking_hash": "7a07e25493"})

        class FakeClient:
            def store(self, payload):
                return 200, {"success": "success", "data": {"hash": "NewReal99",
                             "ticket_number": "2606160001"}}, "{}"
        orig = cps.build_client
        cps.build_client = lambda: FakeClient()
        try:
            service._store_care_panel(t)            # should re-attempt despite internal url
        finally:
            cps.build_client = orig
        t.refresh_from_db()
        self.assertEqual(t.extracted.get("care_panel_ticket_id"), "NewReal99")
        self.assertIsNone(t.extracted.get("internal_tracking"))     # flag cleared
        self.assertEqual(t.tracking_url, "https://care.deodap.in/t?id=NewReal99")

    def test_real_hash_ticket_is_not_restored(self):
        from apps.ingestion import service
        from apps.integrations import care_panel_store as cps

        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="cancel my order",
            tracking_url="https://care.deodap.in/t?id=AlreadyReal",
            extracted={"phone": "9414510548", "care_panel_ticket_id": "AlreadyReal"})
        called = []

        class FakeClient:
            def store(self, payload):
                called.append(payload)
                return 200, {"data": {"hash": "X"}}, "{}"
        orig = cps.build_client
        cps.build_client = lambda: FakeClient()
        try:
            service._store_care_panel(t)
        finally:
            cps.build_client = orig
        self.assertEqual(called, [])                                # never re-stored
        t.refresh_from_db()
        self.assertEqual(t.extracted["care_panel_ticket_id"], "AlreadyReal")


# Force the EXTERNAL Care Panel as the portal base so these tests exercise the Care Panel
# View-Ticket link (customer_ticket_link falls back to it when PUBLIC_BASE_URL is care.deodap.in).
# The OUR-portal link path is covered by tests_mails.ConfirmationTrackingLinkTests.
@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class TrackingEmailTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def test_confirmation_uses_tracking_email_when_url_present(self):
        from apps.ingestion import service
        from apps.tickets.models import Message
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="missing product",
            ticket_number="2606090601",
            tracking_url="https://care.deodap.in/t?id=gKp64KxaAz")
        # created -> M5 "Support Ticket Created Successfully" with the tracking link.
        service.send_confirmation(t, "created")
        out = t.messages.filter(direction=Message.DIRECTION_OUTBOUND).first()
        self.assertEqual(out.subject, "Support Ticket Created Successfully")
        self.assertIn("Your complaint is registered", out.body_text)
        self.assertIn("Ticket ID: 2606090601", out.body_text)
        self.assertIn("View Ticket:\nhttps://care.deodap.in/t?id=gKp64KxaAz", out.body_text)

    def test_m5_includes_view_ticket_url_on_care_panel_success(self):
        # Care Panel creation succeeded (real hash) -> M5 carries the ticket URL in the
        # required "Ticket ID / View Ticket" format.
        from apps.ingestion import service
        from apps.tickets.models import Message
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="damaged item", ticket_number="TKT-2026-000128",
            extracted={"care_panel_ticket_id": "9jA3D6MYlN"})
        service.send_confirmation(t, "created")
        out = t.messages.filter(direction=Message.DIRECTION_OUTBOUND).first()
        self.assertEqual(out.subject, "Support Ticket Created Successfully")
        self.assertIn("Ticket ID: TKT-2026-000128", out.body_text)
        self.assertIn("View Ticket:\nhttps://care.deodap.in/t?id=9jA3D6MYlN", out.body_text)

    def test_m5_omits_url_when_care_panel_failed(self):
        # No Care Panel hash (store-json failed) -> NO url, no 'View Ticket' line (req #4).
        from apps.ingestion import service
        from apps.tickets.models import Message
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="damaged item", ticket_number="TKT-2026-000129")
        service.send_confirmation(t, "created")
        out = t.messages.filter(direction=Message.DIRECTION_OUTBOUND).first()
        self.assertEqual(out.subject, "Support Ticket Created Successfully")
        self.assertIn("Ticket ID: TKT-2026-000129", out.body_text)
        self.assertNotIn("care.deodap.in/t?id=", out.body_text)
        self.assertNotIn("View Ticket:", out.body_text)

    def test_fallback_email_when_no_tracking_url(self):
        from apps.ingestion import service
        from apps.tickets.models import Message
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="missing product")  # no tracking_url
        service.send_confirmation(t, "created")
        out = t.messages.filter(direction=Message.DIRECTION_OUTBOUND).first()
        self.assertEqual(out.subject, "Support Ticket Created Successfully")
        self.assertIn(f"Ticket ID: {t.ticket_id}", out.body_text)   # generic fallback
        self.assertNotIn("care.deodap.in/t?id=", out.body_text)

    def test_matched_uses_existing_ticket_found_email(self):
        from apps.ingestion import service
        from apps.tickets.models import Message
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="missing product",
            ticket_number="2606090601",
            tracking_url="https://care.deodap.in/t?id=gKp64KxaAz")
        service.send_confirmation(t, "updated")
        out = t.messages.filter(direction=Message.DIRECTION_OUTBOUND).first()
        self.assertEqual(out.subject, "Existing Ticket Found")
        self.assertIn("https://care.deodap.in/t?id=gKp64KxaAz", out.body_text)
        self.assertIn("2606090601", out.body_text)


class StorePhoneNormalizationTests(TestCase):
    """store-json rejects a phone > 10 chars ('The phone field must not be greater than 10
    characters.'). The Shopify-verified phone arrives in E.164 (+91...), so the payload must
    send the BARE 10-digit mobile or no data.hash / tracking link is ever returned."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def test_store_phone_strips_country_code(self):
        self.assertEqual(care_panel_store._store_phone("+919847505805"), "9847505805")
        self.assertEqual(care_panel_store._store_phone("919847505805"), "9847505805")
        self.assertEqual(care_panel_store._store_phone("09847505805"), "9847505805")
        self.assertEqual(care_panel_store._store_phone("9847505805"), "9847505805")
        self.assertEqual(care_panel_store._store_phone(""), "")

    def test_payload_phone_is_max_10_chars(self):
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="missing product",
            extracted={"phone": "+919847505805", "order_id": "486324",
                       "customer_name": "vinod KR", "customer_name_source": "shopify_verified"})
        payload = care_panel_store._payload(t)
        self.assertEqual(payload["phone"], "9847505805")
        self.assertLessEqual(len(payload["phone"]), 10)   # store-json hard limit
