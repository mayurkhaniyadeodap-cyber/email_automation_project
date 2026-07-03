"""
Tests for the external DeoDap Care Panel Open-Ticket lookup (phone-keyed find).

    python manage.py test apps.integrations.tests_care_panel
"""

from django.test import TestCase, override_settings

from apps.brand_settings.models import BrandSettings
from apps.integrations import care_panel
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Ticket


class FakeCarePanel:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def lookup(self, *, phone=None, email=None, order_id=None):
        self.calls.append({"phone": phone, "email": email, "order_id": order_id})
        return self.response


class CarePanelSyncTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self, phone="9876543210"):
        extracted = {"order_id": "DD9999"}
        if phone:
            extracted["phone"] = phone
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="buyer@example.com", subject="damaged",
            category="3. Delivery Issues (Post-Delivery)", extracted=extracted,  # subject "damaged" -> issue_id 8
        )

    def test_matched_when_same_issue_open_ticket_exists(self):
        # category 3 (Delivery Issues) -> issue_id 8 (Damaged); the open ticket must be the SAME issue.
        fake = FakeCarePanel({"hasTickets": True, "ticketCount": 1, "tickets": [{
            "id": "BXANEWbOPq", "ticketNumber": "#2502110128", "status": "In-process",
            "issueId": 8, "shopifyOrderNo": None, "email": "buyer@example.com",
            "phone": "9876543210",          # SAME verified phone -> matches the customer
            "url": "https://care.deodap.in/t?id=BXANEWbOPq"}]})
        t = self._ticket()
        cid = care_panel.sync_ticket(t, client=fake)
        self.assertEqual(cid, "BXANEWbOPq")
        t.refresh_from_db()
        self.assertEqual(t.extracted["care_panel_ticket_id"], "BXANEWbOPq")
        self.assertEqual(t.tracking_url, "https://care.deodap.in/t?id=BXANEWbOPq")
        self.assertEqual(t.ticket_number, "2502110128")
        self.assertTrue(t.audit_log.filter(event="care_panel_ticket_matched").exists())

    def test_different_verified_phone_does_not_match(self):
        # The reported bug: same SENDER email but a DIFFERENT verified phone (another customer's
        # order) must NOT link -- create a new ticket. Identity is the phone, not the email.
        fake = FakeCarePanel({"hasTickets": True, "ticketCount": 1, "tickets": [{
            "id": "OTHER", "ticketNumber": "#2606190339", "issueId": 8,
            "shopifyOrderNo": None, "email": "buyer@example.com", "phone": "9999999999",
            "url": "https://care.deodap.in/t?id=OTHER"}]})
        t = self._ticket(phone="9876543210")          # different verified phone
        self.assertIsNone(care_panel.sync_ticket(t, client=fake))
        t.refresh_from_db()
        self.assertEqual(t.tracking_url, "")           # NOT linked to the other customer
        self.assertTrue(t.audit_log.filter(event="care_panel_no_matching_issue").exists())

    def test_different_issue_does_not_match(self):
        # Customer has an open ticket but for a DIFFERENT issue (23 Urgent Delivery)
        # -> must NOT link; returns None so a new ticket is created (the reported bug).
        fake = FakeCarePanel({"hasTickets": True, "ticketCount": 1, "tickets": [{
            "id": "URG1", "ticketNumber": "#2605150332", "issueId": 23,
            "shopifyOrderNo": "DD9999", "url": "https://care.deodap.in/t?id=URG1"}]})
        t = self._ticket()
        self.assertIsNone(care_panel.sync_ticket(t, client=fake))
        t.refresh_from_db()
        self.assertEqual(t.tracking_url, "")           # not linked to the wrong ticket
        self.assertTrue(t.audit_log.filter(event="care_panel_no_matching_issue").exists())

    def test_picks_same_issue_ticket_matching_order(self):
        fake = FakeCarePanel({"hasTickets": True, "ticketCount": 2, "tickets": [
            {"id": "A1", "ticketNumber": "#111", "issueId": 8, "email": "buyer@example.com", "shopifyOrderNo": None,
             "url": "https://care.deodap.in/t?id=A1"},
            {"id": "B2", "ticketNumber": "#222", "issueId": 8, "email": "buyer@example.com", "shopifyOrderNo": "DD9999",
             "url": "https://care.deodap.in/t?id=B2"},
        ]})
        t = self._ticket()
        self.assertEqual(care_panel.sync_ticket(t, client=fake), "B2")  # order match wins
        t.refresh_from_db()
        self.assertEqual(t.ticket_number, "222")

    def test_no_open_ticket(self):
        fake = FakeCarePanel({"hasTickets": False, "ticketCount": 0, "tickets": [],
                              "action": "no_open_tickets", "reply": "No open ticket found."})
        t = self._ticket()
        self.assertIsNone(care_panel.sync_ticket(t, client=fake))
        self.assertTrue(t.audit_log.filter(event="care_panel_no_open_ticket").exists())

    def test_skipped_without_phone(self):
        fake = FakeCarePanel({"hasTickets": False})
        t = self._ticket(phone=None)
        self.assertIsNone(care_panel.sync_ticket(t, client=fake))
        self.assertEqual(fake.calls, [])  # never queried
        self.assertTrue(t.audit_log.filter(event="care_panel_skipped").exists())

    @override_settings(CARE_PANEL_API_URL="https://care.deodap.info/api/external/care-panel/open-tickets",
                       CARE_PANEL_API_KEY="k")
    def test_client_normalizes_full_url(self):
        c = care_panel.build_client_for(self.brand)
        self.assertEqual(c.base_url, "https://care.deodap.info/api/external/care-panel")

    def test_per_brand_overrides_env(self):
        BrandSettings.objects.create(brand=self.brand, integrations={
            "care_panel": {"base_url": "https://brand.example/api", "api_key": "bk"}})
        self.brand.refresh_from_db()
        self.assertEqual(care_panel.build_client_for(self.brand).base_url, "https://brand.example/api")


from apps.classifier.rule_classifier import _extract_phone  # noqa: E402


class PhoneExtractionTests(TestCase):
    def test_extracts_indian_mobile(self):
        self.assertEqual(_extract_phone("call me on 9876543210 please"), "9876543210")
        self.assertEqual(_extract_phone("my number is +91 9876543210"), "9876543210")
        self.assertEqual(_extract_phone("contact 09876543210"), "9876543210")
        self.assertIsNone(_extract_phone("order DD123456 no phone here"))


class CrossCustomerLeakTests(TestCase):
    """A sender who types ANOTHER customer's phone must NOT inherit that customer's
    open ticket / name (the reported Hari Mohan bug)."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def test_phone_match_but_different_email_does_not_link(self):
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="jadavvijay8350@gmail.com", subject="where is my order",
            extracted={"phone": "9582872335"})   # someone else's phone, typed in the mail
        # open-tickets returns Hari Mohan's ticket for that phone -- DIFFERENT email.
        fake = FakeCarePanel({"hasTickets": True, "ticketCount": 1, "tickets": [{
            "id": "EVPxdZQrP4", "ticketNumber": "#2605280613", "name": "Hari Mohan",
            "email": "harimohan1902@gmail.com", "issueId": 1, "shopifyOrderNo": "262203508",
            "url": "https://care.deodap.in/t?id=EVPxdZQrP4"}]})
        self.assertIsNone(care_panel.sync_ticket(t, client=fake))   # NOT linked
        t.refresh_from_db()
        self.assertEqual(t.tracking_url, "")                        # no Hari Mohan data
        self.assertNotIn("care_panel_ticket_id", t.extracted)
        self.assertTrue(t.audit_log.filter(event="care_panel_no_matching_issue").exists())


class ShipmentFlowApiTests(TestCase):
    """The Care Panel shipment-flow request body MUST be exactly
    {topic, trackWith, refNo}, and the response parsed from shipment.status /
    tracking.orderStatus. (The bug: it sent {order_id, phone, email}.)"""

    def setUp(self):
        org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=org, name="DeoDap.in")

    def _fake_requests(self, response_json, captured):
        import json as _json

        class FakeResp:
            status_code = 200
            text = _json.dumps(response_json)
            def json(self_):
                return response_json
            def raise_for_status(self_):
                return None

        class FakeRequests:
            def post(self_, url, headers=None, json=None, timeout=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["body"] = json
                return FakeResp()
        return FakeRequests()

    @override_settings(
        CARE_PANEL_SHIPMENT_URL="https://care.deodap.info/api/external/shipping/shipment-flow",
        CARE_PANEL_API_KEY="khoikho", CARE_PANEL_STORE_TOKEN="")
    def test_request_body_is_exact_format(self):
        from unittest import mock
        captured = {}
        resp = {"shipment": {"status": "Cancelled"}, "tracking": {"orderStatus": "Cancelled"}}
        with mock.patch.object(care_panel, "_requests",
                               lambda: self._fake_requests(resp, captured)):
            norm = care_panel.fetch_shipment_flow(self.brand, "#262098591")
        # EXACT body -- order id stripped of '#'.
        self.assertEqual(captured["body"],
                         {"topic": "shipment_status", "trackWith": "order_no",
                          "refNo": "262098591"})
        self.assertEqual(captured["headers"]["x-api-key"], "khoikho")
        self.assertEqual(captured["url"],
                         "https://care.deodap.info/api/external/shipping/shipment-flow")
        # response parsing: shipment.status / tracking.orderStatus
        self.assertEqual(norm["shipment_status"], "Cancelled")
        self.assertEqual(norm["order_status"], "Cancelled")

    @override_settings(
        CARE_PANEL_SHIPMENT_URL="https://care.deodap.info/api/external/shipping/shipment-flow",
        CARE_PANEL_API_KEY="khoikho", CARE_PANEL_STORE_TOKEN="")
    def test_parses_every_status_value(self):
        from unittest import mock
        for status in ["Cancelled", "NDR", "RTO", "Delivered", "Out For Delivery", "In Transit"]:
            resp = {"shipment": {"status": status}, "tracking": {"orderStatus": status}}
            with mock.patch.object(care_panel, "_requests",
                                   lambda r=resp: self._fake_requests(r, {})):
                norm = care_panel.fetch_shipment_flow(self.brand, "262146052")
            self.assertEqual(norm["shipment_status"], status, status)
            self.assertEqual(norm["order_status"], status, status)


class WebsiteAppIssueMappingTests(TestCase):
    """A Website/App (cat 15) ticket must resolve to its OWN issue type -- never the delivery
    default 'Other Delivery Related Issue' (id 6) (the reported bug)."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self, category, sub_topic, summary="App crashes after login"):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="issue", category=category, sub_topic=sub_topic,
            issue_summary=summary, extracted={"phone": "9895798462"})

    # --- The reported bug: every Website/App input maps to its OWN sub-topic name, the panel's
    #     FINAL issue is that name, and it is NEVER 'Other Items Related Issue'/delivery. -------
    def _final_issue(self, category, sub_topic, summary):
        from apps.integrations.care_panel_store import resolve_issue
        t = self._ticket(category, sub_topic, summary)
        issue_id, issue_name, _src = resolve_issue(t)
        return issue_id, issue_name

    def _assert_link_safe(self, iid):
        # The store-json API needs a NUMERIC issue id, else no data.hash -> no tracking link.
        self.assertTrue(iid.lstrip("-").isdigit(), f"issue_id {iid!r} must be numeric for the link")

    GENERIC = "Website/App Related issues"      # Gallabox label for id 21

    def _detail(self, category, sub_topic, summary):
        from apps.integrations.care_panel_store import _payload
        return _payload(self._ticket(category, sub_topic, summary))["detail"]

    # ALL Website/App sub-topics roll up to the ONE generic Care Panel issue; the SPECIFIC
    # sub-topic is preserved in the ticket detail. ------------------------------------------
    def test_app_crashing_maps_to_website_app_issue(self):
        iid, name = self._final_issue("15. App & Website Technical Issues",
                                      "App Crashing / Not Loading", "App crashing after login")
        self.assertEqual(name, self.GENERIC)                   # FINAL_TICKET_ISSUE (generic)
        self._assert_link_safe(iid)                            # numeric -> link is created
        self.assertTrue(self._detail("15. App & Website Technical Issues",
                                     "App Crashing / Not Loading", "App crashing after login")
                        .startswith("Sub-topic: App Crashing / Not Loading"))

    def test_checkout_maps_to_website_app_issue(self):
        iid, name = self._final_issue("15. App & Website Technical Issues", "", "Checkout page stuck")
        self.assertEqual(name, self.GENERIC)
        self._assert_link_safe(iid)
        self.assertIn("Sub-topic: Checkout Page Not Load",
                      self._detail("15. App & Website Technical Issues", "", "Checkout page stuck"))

    def test_cart_maps_to_website_app_issue(self):
        iid, name = self._final_issue("15. App & Website Technical Issues", "", "Cart not saving items")
        self.assertEqual(name, self.GENERIC)
        self._assert_link_safe(iid)
        self.assertIn("Sub-topic: Cart Not Saving Items",
                      self._detail("15. App & Website Technical Issues", "", "Cart not saving items"))

    ACCOUNT = "Account Related issues"          # Gallabox label for id 20

    def test_otp_maps_to_account_issue(self):
        # OTP / Notifications belongs to the ACCOUNT group (id 20), NOT Website/App (21).
        iid, name = self._final_issue("14. Account & Security",
                                      "OTP / Notifications Not Received", "OTP not received")
        self.assertEqual(iid, "20")
        self.assertEqual(name, self.ACCOUNT)
        self._assert_link_safe(iid)
        self.assertIn("Sub-topic: OTP / Notifications Not Received",
                      self._detail("14. Account & Security",
                                   "OTP / Notifications Not Received", "OTP not received"))

    def test_update_phone_maps_to_account_issue(self):
        iid, name = self._final_issue("14. Account & Security",
                                      "Update Phone / Email", "Update phone number")
        self.assertEqual(iid, "20")
        self.assertEqual(name, self.ACCOUNT)
        self._assert_link_safe(iid)
        self.assertIn("Sub-topic: Update Phone / Email",
                      self._detail("14. Account & Security",
                                   "Update Phone / Email", "Update phone number"))

    def test_saved_address_and_browser_also_roll_up(self):
        for sub, summary in (("Saved Address Not Found", "saved address not found"),
                             ("Browser & Device Support", "browser not supported")):
            with self.subTest(sub=sub):
                iid, name = self._final_issue("15. App & Website Technical Issues", sub, summary)
                self.assertEqual(name, self.GENERIC)
                self._assert_link_safe(iid)

    def test_website_app_issue_never_falls_back_to_other_items(self):
        # FINAL_TICKET_ISSUE is always the generic Website/App issue, never 'Other Items'/delivery,
        # and the id sent is always numeric so the tracking link is created.
        BANNED = {"Other Items Related Issue", "Other Delivery Related Issue", "Tracking Issue",
                  "Missing Item"}
        cases = [("App Crashing / Not Loading", "App crashing after login"),
                 ("Cart Not Saving Items", "cart not saving items"),
                 ("Checkout Page Not Load", "checkout page stuck"),
                 ("Saved Address Not Found", "saved address not found"),
                 ("Browser & Device Support", "browser not supported"),
                 ("", "app crashing after login")]
        for sub, summary in cases:
            with self.subTest(sub=sub, summary=summary):
                iid, name = self._final_issue("15. App & Website Technical Issues", sub, summary)
                self.assertEqual(name, self.GENERIC)
                self.assertNotIn(name, BANNED, f"{summary!r} -> {name!r}")
                self._assert_link_safe(iid)

    def test_default_maps_to_real_gallabox_website_app_id(self):
        # The default config already sends the REAL Gallabox id 21 ("Website/App Related issues")
        # -- so the panel shows the correct label AND the link works, with no extra config.
        iid, name = self._final_issue("15. App & Website Technical Issues",
                                      "App Crashing / Not Loading", "app crashing")
        self.assertEqual(iid, "21")
        self.assertEqual(name, "Website/App Related issues")

    def test_payload_sends_generic_issue_name_for_all_website_app(self):
        # The store-json payload carries the DISPLAYED issue name = the generic Website/App issue
        # for the 5 Website/App sub-topics, while issue_id (the link) is unchanged + numeric.
        from apps.integrations.care_panel_store import _payload
        cases = [("15. App & Website Technical Issues", "App Crashing / Not Loading", "App crashing after login"),
                 ("15. App & Website Technical Issues", "Cart Not Saving Items", "cart not saving"),
                 ("15. App & Website Technical Issues", "Checkout Page Not Load", "checkout page stuck"),
                 ("15. App & Website Technical Issues", "Saved Address Not Found", "saved address not found"),
                 ("15. App & Website Technical Issues", "Browser & Device Support", "browser not supported")]
        for cat, sub, summary in cases:
            with self.subTest(sub=sub):
                p = _payload(self._ticket(cat, sub, summary))
                self.assertEqual(p["issue"], self.GENERIC)
                self.assertEqual(p["issue_name"], self.GENERIC)

    def test_payload_sends_account_issue_name_for_account_subtopics(self):
        # Account sub-topics carry the Account Related issues name (id 20).
        from apps.integrations.care_panel_store import _payload
        for sub in ("OTP / Notifications Not Received", "Update Phone / Email", "Delete Account"):
            with self.subTest(sub=sub):
                p = _payload(self._ticket("14. Account & Security", sub, sub))
                self.assertEqual(p["issue"], self.ACCOUNT)
                self.assertEqual(p["issue_name"], self.ACCOUNT)
                self.assertIn(f"Sub-topic: {sub}", p["detail"])
                self.assertTrue(p["issue_id"].lstrip("-").isdigit())     # link intact
                self.assertTrue(p["detail"].startswith(f"Sub-topic: {sub}"))

    def test_payload_issue_name_is_catalog_name_for_delivery(self):
        # A real delivery ticket keeps its Care Panel catalog issue name (NOT the generic one).
        from apps.integrations.care_panel_store import _payload
        p = _payload(self._ticket("3. Delivery Issues", "Damaged Item", summary="my order is damaged"))
        self.assertNotEqual(p["issue"], self.GENERIC)
        self.assertEqual(p["detail"], "my order is damaged")

    def test_issue_type_still_detected_for_logs(self):
        # The issue TYPE is still locked to the cat-15 sub-topic (used for CARE-PANEL-ISSUE log).
        from apps.integrations.care_panel_store import _detect_issue_type
        t = self._ticket("15. App & Website Technical Issues", "App Crashing / Not Loading")
        self.assertEqual(_detect_issue_type(t)[0], "App Crashing / Not Loading")

    def test_classification_and_care_panel_logs_emitted(self):
        # CLASSIFICATION_SUBTOPIC, CARE_PANEL_ISSUE and FINAL_TICKET_ISSUE logged before store.
        from apps.integrations.care_panel_store import resolve_issue
        t = self._ticket("15. App & Website Technical Issues", "App Crashing / Not Loading")
        with self.assertLogs("apps.integrations.care_panel_store", level="INFO") as cm:
            resolve_issue(t)
        blob = "\n".join(cm.output)
        self.assertIn("CLASSIFICATION_SUBTOPIC=App Crashing / Not Loading", blob)
        self.assertIn("CARE_PANEL_ISSUE=Website/App Related issues", blob)
        self.assertIn("FINAL_TICKET_ISSUE=Website/App Related issues", blob)
        self.assertIn("CARE_PANEL_ISSUE_ID=21", blob)
        self.assertNotIn("FINAL_TICKET_ISSUE=Other Items Related Issue", blob)

    def test_payload_detail_surfaces_subtopic_for_website_app(self):
        # The Care Panel "Issue" is the single generic label, so the SPECIFIC sub-topic is
        # surfaced in the ticket DETAIL -> the agent always sees 'App Crashing / Not Loading'.
        from apps.integrations.care_panel_store import _payload
        t = self._ticket("15. App & Website Technical Issues", "App Crashing / Not Loading")
        p = _payload(t)
        self.assertTrue(p["detail"].startswith("Sub-topic: App Crashing / Not Loading"))
        self.assertTrue(p["issue_id"].lstrip("-").isdigit())

    def test_payload_detail_unchanged_for_delivery(self):
        from apps.integrations.care_panel_store import _payload
        t = self._ticket("3. Delivery Issues", "Damaged Item", summary="my order is damaged")
        self.assertEqual(_payload(t)["detail"], "my order is damaged")

    def test_attachments_do_not_change_issue(self):
        # Screenshot + video present must NOT flip the issue to a delivered-item / multi id.
        from apps.integrations.care_panel_store import _detect_issue_type, resolve_issue
        t = self._ticket("15. App & Website Technical Issues", "App Crashing / Not Loading")
        t.extracted = {**t.extracted, "has_photo": True, "has_video": True,
                       "requires_evidence": True}
        t.issue_summary = "App crashing after login (screenshot + video attached)"
        t.save()
        self.assertEqual(_detect_issue_type(t)[0], "App Crashing / Not Loading")
        self.assertEqual(resolve_issue(t)[1], "Website/App Related issues")

    def test_real_ids_are_used_when_configured(self):
        # The admin sets the Care Panel's real numeric id -> it is sent (env override wins).
        from django.test import override_settings
        from apps.integrations.care_panel_store import resolve_issue
        real_map = dict(__import__("django.conf", fromlist=["settings"]).settings.CARE_PANEL_ISSUE_MAP)
        real_map["App Crashing / Not Loading"] = "24"
        with override_settings(CARE_PANEL_ISSUE_MAP=real_map):
            t = self._ticket("15. App & Website Technical Issues", "App Crashing / Not Loading")
            self.assertEqual(resolve_issue(t)[0], "24")

    def test_all_website_app_subtopics_locked(self):
        from apps.integrations.care_panel_store import _detect_issue_type
        for sub in ("App Crashing / Not Loading", "Cart Not Saving Items",
                    "Checkout Page Not Load", "Saved Address Not Found",
                    "Browser & Device Support", "Update Phone / Email",
                    "OTP / Notifications Not Received"):
            with self.subTest(sub=sub):
                t = self._ticket("15. App & Website Technical Issues", sub)
                self.assertEqual(_detect_issue_type(t)[0], sub)

    def test_account_subtopics_grouped_with_website_app(self):
        # Update Phone / Email and OTP live under cat 14 in our taxonomy but the spec groups
        # them with Website/App -> still resolve to their own issue type, not delivery.
        from apps.integrations.care_panel_store import _detect_issue_type
        t = self._ticket("14. Account & Security", "Update Phone / Email")
        self.assertEqual(_detect_issue_type(t)[0], "Update Phone / Email")

    @override_settings(PUBLIC_BASE_URL="https://care.deodap.in")   # exercise the Care Panel link
    def test_cat15_confirmation_email_contains_link_once_hash_exists(self):
        # PROOF: once the Care Panel store returns a hash, the cat-15 confirmation email carries
        # the tracking link.
        from apps.ingestion import service
        from apps.tickets.models import Message
        t = self._ticket("15. App & Website Technical Issues", "Checkout Page Not Load")
        t.ticket_number = "TKT-2026-000205"
        t.extracted = {**t.extracted, "care_panel_ticket_id": "XYZhash205"}
        t.save()
        service.send_confirmation(t, "created")
        body = (t.messages.filter(direction=Message.DIRECTION_OUTBOUND)
                .order_by("created_at").last().body_text)
        self.assertIn("https://care.deodap.in/t?id=XYZhash205", body)
        self.assertIn("TKT-2026-000205", body)

    def test_cat15_unknown_subtopic_defaults_to_app_crash_not_delivery(self):
        from apps.integrations.care_panel_store import _detect_issue_type
        # No sub-topic AND no website/app keyword in the text -> the cat-15 app-crash default.
        t = self._ticket("15. App & Website Technical Issues", "", summary="please help me")
        issue_type, source = _detect_issue_type(t)
        self.assertEqual(issue_type, "App Crashing / Not Loading")
        self.assertEqual(source, "website_app_default")

    def test_delivery_ticket_still_maps_correctly(self):
        # Regression: a real damage ticket is unaffected by the website/app branch.
        from apps.integrations.care_panel_store import _detect_issue_type
        t = self._ticket("3. Delivery Issues", "Damaged Item", summary="my order is damaged")
        self.assertEqual(_detect_issue_type(t)[0], "Damaged Item")
