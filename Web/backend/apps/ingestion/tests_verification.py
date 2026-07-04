"""
Two-step verification inquiries (Franchise / Dropshipping / Company Profile / Invoice):
  STEP 1 first email  -> M_VERIFY_REQUEST acknowledgement, PendingConversation, NO ticket.
  STEP 2-3 reply OK   -> verify identifier -> create ticket -> close pending.
  STEP 4 reply BAD    -> M_VERIFY_FAILED, pending stays open.

    python manage.py test apps.ingestion.tests_verification
"""

from django.test import TestCase, override_settings

from apps.classifier.service import ClassificationResult
from apps.ingestion import service
from apps.ingestion.tests_imap import FakeImap
from apps.ingestion.tests_smart import eml
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category
from apps.tickets.models import Message, PendingConversation, Ticket


class FakeShopify:
    def __init__(self, orders=None, by_phone=None, by_email=None):
        self.orders = orders or {}
        self.by_phone = by_phone or {}
        self.by_email = by_email or {}

    def get_order(self, order_id):
        return self.orders.get(order_id)

    def recent_orders_by_phone(self, phone, limit=5):
        return self.by_phone.get(phone, [])

    def recent_orders_by_email(self, email, limit=5):
        return self.by_email.get(email, [])


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class VerificationFlowTests(TestCase):
    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        self.cat11 = Category.objects.create(brand=self.brand, code="11",
                                             name="Wholesale / Bulk Purchase (B2B)")
        self.cat8 = Category.objects.create(brand=self.brand, code="8", name="Payment & Invoice")

    def _classify(self, category, cat_ref):
        return lambda b, m: ClassificationResult(
            category=category, sub_topic="", confidence=0.8, extracted={}, sentiment="neutral",
            language="en", is_support_request=True, issue_summary="inquiry",
            requires_evidence=False, requires_agent=False, category_ref=cat_ref,
            sub_topic_ref=None)

    def _run(self, *emails, classify, clients=None):
        from apps.integrations import context as ctx
        self.sent = []
        clients = clients or {"shopify": None, "shipping": None, "gokwik": None}
        oc, ob, oe = service._classify_dict, ctx.build_clients, service._send_customer_email
        service._classify_dict = classify
        ctx.build_clients = lambda settings: clients
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body}) or "<sent>")
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            service._classify_dict, ctx.build_clients, service._send_customer_email = oc, ob, oe

    def _last(self):
        return self.sent[-1] if self.sent else None

    # STEP 1 -----------------------------------------------------------------------------

    def _promote(self, first, reply, classify, clients=None):
        self._run(first, reply, classify=classify, clients=clients)
        return Ticket.objects.get()

    def test_store_resolves_phone_from_order_when_missing(self):
        # The reported bug: an evidence-flow ticket created from an order_id WITHOUT a phone -> the
        # store was skipped (no_phone) -> no link. _store_care_panel must resolve the order owner
        # from Shopify, stamping phone + verified name so the link can be created.
        from apps.integrations import context as ctx
        order = {"order_id": "262356376", "shipped": True, "customer_name": "Mohammed Anas",
                 "customer_phone": "9876500011", "customer_email": "owner@shop.com"}
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="sender@gmail.com", subject="damaged", category="3. Delivery Issues",
            sub_topic="Damaged Item", issue_summary="damaged raincoat",
            extracted={"order_id": "262356376"})       # order_id present, NO phone
        ob = ctx.build_clients
        ctx.build_clients = lambda s: {"shopify": FakeShopify(orders={"262356376": order}),
                                       "shipping": None, "gokwik": None}
        try:
            service._store_care_panel(t)
        finally:
            ctx.build_clients = ob
        t.refresh_from_db()
        e = t.extracted or {}
        self.assertEqual(e.get("phone"), "9876500011")               # phone resolved -> link works
        self.assertEqual(e.get("customer_name"), "Mohammed Anas")    # verified owner name
        self.assertEqual(e.get("customer_name_source"), "shopify_verified")

    def test_store_resolves_name_when_phone_present_but_name_unknown(self):
        # The reported bug: damaged-item ticket has a TYPED phone + order_id but NO verified name
        # -> Care Panel showed "Unknown". The owner name must be resolved from the order anyway.
        from apps.integrations import context as ctx
        order = {"order_id": "262295437", "shipped": True, "customer_name": "Fazeela Beegum",
                 "customer_phone": "9562138449", "customer_email": "fazeela@shop.com"}
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="sender@gmail.com", subject="damaged",
            category="7. Return, Refund & Replacement", sub_topic="Damaged Item",
            issue_summary="damaged order", extracted={"order_id": "262295437",
                                                      "phone": "9562138449"})  # phone present, NO name
        ob = ctx.build_clients
        ctx.build_clients = lambda s: {"shopify": FakeShopify(orders={"262295437": order}),
                                       "shipping": None, "gokwik": None}
        try:
            service._store_care_panel(t)
        finally:
            ctx.build_clients = ob
        t.refresh_from_db()
        e = t.extracted or {}
        self.assertEqual(e.get("customer_name"), "Fazeela Beegum")   # no longer "Unknown"
        self.assertEqual(e.get("customer_name_source"), "shopify_verified")

    def test_payment_evidence_request_asks_for_screenshot_not_item_photo(self):
        # A Payment Issue must ask for a PAYMENT SCREENSHOT, never "a clear photo of the item".
        from apps.tickets.models import PendingConversation
        self.sent = []
        oe = service._send_customer_email
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body}) or "<m>")
        try:
            p = PendingConversation.objects.create(
                organization=self.org, brand=self.brand, mailbox=self.mailbox,
                customer_email="b@x.com", subject="payment",
                category="8. Payment & Invoice", sub_topic="Payment Deducted But Order Not Placed",
                issue_summary="payment deducted but order not placed", body_text="paid no order")
            service._send_photo_request(self.mailbox, {}, p)
        finally:
            service._send_customer_email = oe
        body = self.sent[-1]["body"]
        self.assertIn("Payment Screenshot (Mandatory)", body)
        self.assertNotIn("photo of the item", body)

    def test_genuine_payment_issue_maps_to_cyberfraud_12(self):
        # A payment complaint -> Care Panel id 12 "CyberFraud Report" (there is no valid id 6 in
        # the real catalog, so payment problems are filed under CyberFraud per the brand).
        from apps.integrations.care_panel_store import resolve_issue
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="I was charged twice",
            category="8. Payment & Invoice", issue_summary="double charge payment", extracted={})
        self.assertEqual(resolve_issue(t)[0], "12")
        self.assertEqual(resolve_issue(t)[1], "CyberFraud Report")

    def test_order_id_extracted_from_colon_phrasing(self):
        # Prove extraction handles "my order id is : 262241305" (the exact reply).
        from apps.classifier.rule_classifier import _extract_order_id
        self.assertEqual(_extract_order_id("my order id is : 262241305"), "262241305")

@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class UniversalFirstEmailVerificationTests(VerificationFlowTests):
    """Universal rule: a FIRST email that already carries a VALID identifier is verified +
    processed immediately (NO verification email). An invalid / missing identifier -> the
    verification email. Covers the 11 requested scenarios."""

    ORDER = {"order_id": "486324", "shipped": True}

    def _shop(self):
        return FakeShopify(
            orders={"486324": dict(self.ORDER)},
            by_phone={"9876543210": [dict(self.ORDER)]},
            by_email={"buyer@shop.com": [dict(self.ORDER)]})

    def _clients(self):
        return {"shopify": self._shop(), "shipping": None, "gokwik": None}

    def _outbound_text(self, ticket):
        # Concatenate ALL outbound bodies -- a promoted ticket may carry both the engine
        # draft and the M5_INQUIRY confirmation, so we don't depend on message ordering.
        return "\n".join(
            ticket.messages.filter(direction=Message.DIRECTION_OUTBOUND)
            .values_list("body_text", flat=True))

    # === 1-3: first email with a VALID identifier -> immediate processing, NO verify email ==

    def test_tracking_success_flow(self):
        cat1 = Category.objects.create(brand=self.brand, code="1",
                                       name="Shipment & Delivery Tracking")
        self._run(eml(subject="where is my order",
                      body="where is my order? order number 486324", message_id="<a@x>"),
                  clients=self._clients(),
                  classify=self._classify("1. Shipment & Delivery Tracking", cat1))
        body = self._last()["body"]
        self.assertIn("Here is the latest status", body)
        self.assertIn("Order ID: 486324", body)
        self.assertEqual(Ticket.objects.get().status, Ticket.STATUS_AUTO_RESOLVED)

    # === 8-11: inquiry success flows -> ticket + category-specific confirmation =============

@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class UniversalEvidenceWorkflowTests(VerificationFlowTests):
    """Evidence categories must VERIFY (Shopify) BEFORE we ask for proof, then create the
    ticket once proof arrives. Verify-soft: already-attached proof is accepted. Video is
    mandatory for Defective/Wrong/Missing; photo is enough for Damaged/Quality/Quantity."""

    MATCH_PHONE = "9974637387"

    def setUp(self):
        super().setUp()
        self.cat3 = Category.objects.create(brand=self.brand, code="3",
                                            name="Delivery Issues (Post-Delivery)")
        self.cat7 = Category.objects.create(brand=self.brand, code="7",
                                            name="Return, Refund & Replacement")

    def _clients(self, match=True):
        order = {"order_id": "262098591", "shipped": True}
        by_phone = {self.MATCH_PHONE: [order]} if match else {}
        return {"shopify": FakeShopify(orders={"262098591": order}, by_phone=by_phone),
                "shipping": None, "gokwik": None}

    def _classify_ev(self, category, summary, cat_ref):
        return lambda b, m: ClassificationResult(
            category=category, sub_topic="", confidence=0.9, extracted={}, sentiment="neutral",
            language="en", is_support_request=True, issue_summary=summary,
            requires_evidence=True, requires_agent=False, category_ref=cat_ref, sub_topic_ref=None)

    # --- Damaged (photo category) -----------------------------------------------------
    def test_damage_verified_then_photo_creates_ticket(self):
        self._run(
            eml(subject="my order is damaged",
                body=f"the item is damaged. my mobile is {self.MATCH_PHONE}", message_id="<a@x>"),
            # Damaged requires BOTH a photo AND a video -> supply both.
            eml(subject="Re: my order is damaged", body="here is the photo and video",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>",
                image=True, video=True),
            clients=self._clients(match=True),
            classify=self._classify_ev("3. Delivery Issues (Post-Delivery)", "item is damaged",
                                       self.cat3))
        # first email: verified -> asked for proof, NO ticket; reply with proof -> ticket.
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(PendingConversation.objects.count(), 0)

    def test_damage_verified_then_video_creates_ticket(self):
        self._run(
            eml(subject="my order is damaged",
                body=f"damaged item. mobile {self.MATCH_PHONE}", message_id="<a@x>"),
            # Damaged requires BOTH a photo AND a video -> supply both.
            eml(subject="Re: my order is damaged", body="photo and video attached",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>",
                image=True, video=True),
            clients=self._clients(match=True),
            classify=self._classify_ev("3. Delivery Issues (Post-Delivery)", "item is damaged",
                                       self.cat3))
        self.assertEqual(Ticket.objects.count(), 1)

    def test_damage_unverified_requests_identifier(self):
        self._run(
            eml(subject="my order is damaged",
                body="the item is damaged. mobile 9999999999", message_id="<a@x>"),
            clients=self._clients(match=False),       # Shopify configured, NO match
            classify=self._classify_ev("3. Delivery Issues (Post-Delivery)", "item is damaged",
                                       self.cat3))
        self.assertEqual(Ticket.objects.count(), 0)
        body = self._last()["body"]
        self.assertIn("could not verify", body)        # asks for an identifier (STEP 7)
        self.assertNotIn("photo", body.lower())        # NOT a photo/video request yet
        self.assertTrue((PendingConversation.objects.get().extracted or {})
                        .get("awaiting_verification"))

    # --- Missing / Wrong (video-mandatory) --------------------------------------------
    def test_missing_item_verified_then_video_creates_ticket(self):
        self._run(
            eml(subject="missing item", body=f"item missing from order. mobile {self.MATCH_PHONE}",
                message_id="<a@x>"),
            # Missing requires BOTH a photo (POS paper) AND a video -> supply both.
            eml(subject="Re: missing item", body="photo and unboxing video", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True, video=True),
            clients=self._clients(match=True),
            classify=self._classify_ev("3. Delivery Issues (Post-Delivery)",
                                       "missing item not received", self.cat3))
        self.assertEqual(Ticket.objects.count(), 1)

    def test_wrong_item_verified_then_video_creates_ticket(self):
        self._run(
            eml(subject="wrong item", body=f"wrong item delivered. mobile {self.MATCH_PHONE}",
                message_id="<a@x>"),
            # Wrong item requires BOTH a photo AND a video -> supply both.
            eml(subject="Re: wrong item", body="photo and video", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True, video=True),
            clients=self._clients(match=True),
            classify=self._classify_ev("3. Delivery Issues (Post-Delivery)",
                                       "wrong item delivered", self.cat3))
        self.assertEqual(Ticket.objects.count(), 1)

    def test_missing_item_video_mandatory_photo_not_enough(self):
        # Decision 2: a photo does NOT satisfy a video-mandatory category -> still asks.
        self._run(
            eml(subject="missing item", body=f"item missing. mobile {self.MATCH_PHONE}",
                message_id="<a@x>"),
            eml(subject="Re: missing item", body="photo only", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True),
            clients=self._clients(match=True),
            classify=self._classify_ev("3. Delivery Issues (Post-Delivery)",
                                       "missing item not received", self.cat3))
        self.assertEqual(Ticket.objects.count(), 0)        # photo not enough for video category
        self.assertEqual(PendingConversation.objects.get().status, "waiting_for_video")

    # --- Quality (photo category) -----------------------------------------------------
    def test_quality_issue_verified_then_photo_creates_ticket(self):
        self._run(
            eml(subject="bad quality", body=f"poor quality product. mobile {self.MATCH_PHONE}",
                message_id="<a@x>"),
            eml(subject="Re: bad quality", body="photo", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True),
            clients=self._clients(match=True),
            classify=self._classify_ev("3. Delivery Issues (Post-Delivery)",
                                       "bad quality item", self.cat3))
        self.assertEqual(Ticket.objects.count(), 1)

    # --- No-loop guards ---------------------------------------------------------------
    def test_no_duplicate_ticket_after_evidence(self):
        # Damaged requires BOTH a photo AND a video -> supply both on the (repeated) reply.
        reply = eml(subject="Re: my order is damaged", body="photo and video",
                    message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>",
                    image=True, video=True)
        self._run(
            eml(subject="my order is damaged",
                body=f"damaged. mobile {self.MATCH_PHONE}", message_id="<a@x>"),
            reply, reply,                                  # SAME reply Message-ID twice
            clients=self._clients(match=True),
            classify=self._classify_ev("3. Delivery Issues (Post-Delivery)", "item is damaged",
                                       self.cat3))
        self.assertEqual(Ticket.objects.count(), 1)        # never a duplicate

    def test_photo_and_video_together_no_second_request(self):
        # BUG 1: customer replies with BOTH photo and video -> ticket created, and NO further
        # evidence-request email is ever sent (the engine must not re-ask).
        self._run(
            eml(subject="my order is damaged",
                body=f"damaged. mobile {self.MATCH_PHONE}", message_id="<a@x>"),
            eml(subject="Re: my order is damaged", body="photo and video attached",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>",
                image=True, video=True),
            clients=self._clients(match=True),
            classify=self._classify_ev("3. Delivery Issues (Post-Delivery)", "item is damaged",
                                       self.cat3))
        self.assertEqual(Ticket.objects.count(), 1)                 # ticket created
        # exactly ONE evidence request total (the first ask); none after evidence arrived.
        asks = [s for s in self.sent if "photo" in s["body"].lower() or "video" in s["body"].lower()]
        self.assertEqual(len(asks), 1)

    def test_no_repeat_evidence_request(self):
        self._run(
            eml(subject="my order is damaged",
                body=f"damaged. mobile {self.MATCH_PHONE}", message_id="<a@x>"),
            # Damaged requires BOTH a photo AND a video -> supply both on the reply.
            eml(subject="Re: my order is damaged", body="photo and video", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True, video=True),
            eml(subject="Re: my order is damaged", body="any update?", message_id="<a3@x>",
                in_reply_to="<a@x>", references="<a@x>"),
            clients=self._clients(match=True),
            classify=self._classify_ev("3. Delivery Issues (Post-Delivery)", "item is damaged",
                                       self.cat3))
        # The EV_DAMAGED evidence request ("...register your complaint...") is sent exactly
        # ONCE (the first ask); once the proof arrives it is never re-requested.
        proof_asks = [s for s in self.sent if "register your complaint" in s["body"].lower()]
        self.assertEqual(len(proof_asks), 1)               # asked for proof exactly ONCE
        self.assertEqual(Ticket.objects.count(), 1)

    # --- Parity: a phone that verifies for Tracking verifies for Evidence -------------
    def test_same_phone_verifies_identically_across_workflows(self):
        # The reported inconsistency: the SAME mobile must produce the SAME verdict in every
        # workflow, because all of them go through the one shared _shopify_verify -> the same
        # lookup_tracking -> recent_orders_by_phone. No divergent lookup / normalization.
        from apps.integrations import context as ctx
        ob = ctx.build_clients
        ctx.build_clients = lambda settings: self._clients(match=True)
        try:
            results = {wf: service._shopify_verify(self.brand, "", self.MATCH_PHONE, "",
                                                   workflow=wf)[0]
                       for wf in ("tracking", "evidence", "invoice")}
        finally:
            ctx.build_clients = ob
        self.assertEqual(results, {"tracking": "verified", "evidence": "verified",
                                   "invoice": "verified"})

    def test_evidence_with_tracking_phone_asks_for_proof_not_identifier(self):
        # End-to-end: the same matching mobile on an Evidence email -> verified -> asks for
        # the proof, NEVER "could not verify".
        self._run(eml(subject="my order is missing",
                      body=f"my order is missing. my mobile is {self.MATCH_PHONE}",
                      message_id="<e@x>"),
                  clients=self._clients(match=True),
                  classify=self._classify_ev("3. Delivery Issues (Post-Delivery)",
                                             "missing item not received", self.cat3))
        last = self._last()["body"]
        self.assertNotIn("could not verify", last)
        self.assertIn("video", last.lower())

    # --- Non-evidence category: immediate ticket --------------------------------------
    def test_verified_non_evidence_category_creates_ticket_immediately(self):
        cat7, _ = Category.objects.get_or_create(
            brand=self.brand, code="7", defaults={"name": "Return, Refund & Replacement"})
        shop = FakeShopify(orders={"262134021": {"order_id": "262134021", "shipped": True}})
        self._run(
            eml(subject="refund request", body="please refund my order number 262134021",
                message_id="<a@x>"),
            clients={"shopify": shop, "shipping": None, "gokwik": None},
            classify=self._classify("7. Return, Refund & Replacement", cat7))
        self.assertEqual(Ticket.objects.count(), 1)        # no evidence step, immediate ticket
        self.assertEqual(PendingConversation.objects.count(), 0)


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class UniversalShopifyVerificationTests(TestCase):
    """OR logic: a customer is VERIFIED if ANY ONE of order / mobile / email matches Shopify.
    Every workflow routes through the shared service._shopify_verify -> lookup_tracking, which
    now tries all three identifiers (was if/elif/else -- the bug)."""

    ORDER = {"order_id": "262098591", "shipped": True}
    OID, PHONE, EMAIL = "262098591", "6358956674", "customer@gmail.com"

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")

    def _shop(self):
        return FakeShopify(orders={self.OID: dict(self.ORDER)},
                           by_phone={self.PHONE: [dict(self.ORDER)]},
                           by_email={self.EMAIL: [dict(self.ORDER)]})

    def _verify(self, workflow="evidence", order="", phone="", email=""):
        from apps.integrations import context as ctx
        ob = ctx.build_clients
        ctx.build_clients = lambda s: {"shopify": self._shop(), "shipping": None, "gokwik": None}
        try:
            return service._shopify_verify(self.brand, order, phone, email, workflow=workflow)
        finally:
            ctx.build_clients = ob

    def _assert_verified(self, workflow, **ids):
        status, info = self._verify(workflow=workflow, **ids)
        self.assertEqual(status, "verified")
        return info

    # --- STEP 2: each identifier verifies on its own (OR logic) ------------------------
    def test_order_number_verifies(self):
        self.assertEqual(self._assert_verified("evidence", order=self.OID)["matched_by"], "order_id")

    def test_mobile_number_verifies(self):
        self.assertEqual(self._assert_verified("evidence", phone=self.PHONE)["matched_by"], "mobile")

    def test_email_verifies(self):
        self.assertEqual(self._assert_verified("evidence", email=self.EMAIL)["matched_by"], "email")

    def test_order_or_mobile_or_email_logic(self):
        # Wrong order but valid mobile -> still verified (OR, never AND).
        self.assertEqual(self._verify(order="999999", phone=self.PHONE)[1]["matched_by"], "mobile")
        # Wrong order + wrong mobile but valid email -> verified by email.
        self.assertEqual(
            self._verify(order="999999", phone="9999999999", email=self.EMAIL)[1]["matched_by"],
            "email")

    def test_verification_fails_only_when_all_three_fail(self):
        status, _ = self._verify(order="999999", phone="9999999999", email="nobody@nowhere.com")
        self.assertEqual(status, "not_found")
        # ANY one valid identifier -> verified.
        self.assertEqual(self._verify(order=self.OID)[0], "verified")
        self.assertEqual(self._verify(phone=self.PHONE)[0], "verified")
        self.assertEqual(self._verify(email=self.EMAIL)[0], "verified")

    # --- STEP 3: every workflow verifies by order / mobile / email ---------------------
    def test_tracking_verifies_by_order(self):  self._assert_verified("tracking", order=self.OID)
    def test_tracking_verifies_by_mobile(self): self._assert_verified("tracking", phone=self.PHONE)
    def test_tracking_verifies_by_email(self):  self._assert_verified("tracking", email=self.EMAIL)

    def test_damage_verifies_by_order(self):    self._assert_verified("evidence", order=self.OID)
    def test_damage_verifies_by_mobile(self):   self._assert_verified("evidence", phone=self.PHONE)
    def test_damage_verifies_by_email(self):    self._assert_verified("evidence", email=self.EMAIL)

    def test_wrong_item_verifies_by_order(self):  self._assert_verified("evidence", order=self.OID)
    def test_wrong_item_verifies_by_mobile(self): self._assert_verified("evidence", phone=self.PHONE)
    def test_wrong_item_verifies_by_email(self):  self._assert_verified("evidence", email=self.EMAIL)

    def test_missing_item_verifies_by_order(self):  self._assert_verified("evidence", order=self.OID)
    def test_missing_item_verifies_by_mobile(self): self._assert_verified("evidence", phone=self.PHONE)
    def test_missing_item_verifies_by_email(self):  self._assert_verified("evidence", email=self.EMAIL)

    def test_invoice_verifies_by_order(self):   self._assert_verified("invoice", order=self.OID)
    def test_invoice_verifies_by_mobile(self):  self._assert_verified("invoice", phone=self.PHONE)
    def test_invoice_verifies_by_email(self):   self._assert_verified("invoice", email=self.EMAIL)


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class VerifiedCustomerNameTests(VerificationFlowTests):
    """A verified ticket's customer name must be the Shopify ORDER OWNER, never the email
    sender's display name (the reported bug: ticket showed 'Chintan Dabhi' for an order owned
    by 'Raneesh Kanhirakadan')."""

    MATCH_PHONE = "9562110003"
    SHOPIFY_NAME = "Raneesh Kanhirakadan"
    SENDER = "Chintan Dabhi <dabhichintan2134@gmail.com>"

    def setUp(self):
        super().setUp()
        self.cat3 = Category.objects.create(brand=self.brand, code="3",
                                            name="Delivery Issues (Post-Delivery)")

    def _clients(self, name="Raneesh Kanhirakadan"):
        order = {"order_id": "262339239", "shipped": True}
        if name:
            order["customer_name"] = name
        return {"shopify": FakeShopify(orders={"262339239": order},
                                       by_phone={self.MATCH_PHONE: [order]}),
                "shipping": None, "gokwik": None}

    def _classify_ev(self, summary):
        # AI mis-extracts the SENDER name -- verification must override it.
        return lambda b, m: ClassificationResult(
            category="3. Delivery Issues (Post-Delivery)", sub_topic="", confidence=0.9,
            extracted={"customer_name": "Chintan Dabhi"}, sentiment="neutral", language="en",
            is_support_request=True, issue_summary=summary, requires_evidence=True,
            requires_agent=False, category_ref=self.cat3, sub_topic_ref=None)

    def _damaged_flow(self, summary="item is damaged", shopify_name="Raneesh Kanhirakadan"):
        # Damaged / Wrong / Missing now ALL require BOTH a photo AND a video before the pending
        # promotes to a ticket -- attach both so the flow always produces a ticket.
        self._run(
            eml(subject="my order issue",
                body=f"problem. my mobile is {self.MATCH_PHONE}", message_id="<a@x>",
                from_addr=self.SENDER),
            eml(subject="Re: my order issue", body="here is proof", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>",
                image=True, video=True, from_addr=self.SENDER),
            clients=self._clients(name=shopify_name),
            classify=self._classify_ev(summary))
        return Ticket.objects.get()

    def _name(self, ticket):
        from apps.integrations.care_panel_store import _customer_name
        return _customer_name(ticket)

    def test_sender_name_different_from_shopify_uses_shopify(self):
        t = self._damaged_flow()
        self.assertEqual(t.extracted.get("customer_name"), self.SHOPIFY_NAME)
        self.assertEqual(t.extracted.get("customer_name_source"), "shopify_verified")
        self.assertEqual(self._name(t), self.SHOPIFY_NAME)
        self.assertNotEqual(self._name(t), "Chintan Dabhi")

    def test_sender_name_same_as_shopify(self):
        t = self._damaged_flow(shopify_name="Chintan Dabhi")
        self.assertEqual(self._name(t), "Chintan Dabhi")

    def test_no_shopify_customer_name_is_unknown_not_sender(self):
        # Order has NO customer name -> name is blank/Unknown, NEVER the email sender.
        t = self._damaged_flow(shopify_name="")
        self.assertEqual(self._name(t), "Unknown")
        self.assertNotEqual(self._name(t), "Chintan Dabhi")

    def test_wrong_item_uses_shopify_name(self):
        t = self._damaged_flow(summary="wrong item delivered")
        self.assertEqual(self._name(t), self.SHOPIFY_NAME)

    def test_missing_item_uses_shopify_name(self):
        t = self._damaged_flow(summary="missing item not received")
        self.assertEqual(self._name(t), self.SHOPIFY_NAME)

    def test_unverified_customer_name_is_unknown_never_sender_header(self):
        # No Shopify-verified name -> 'Unknown'. The email sender's display name (From
        # header) must NEVER become the ticket customer name.
        from apps.integrations.care_panel_store import _customer_name
        from apps.tickets.models import Message
        t = Ticket.objects.create(organization=self.org, brand=self.brand, mailbox=self.mailbox,
                                  customer_email="dabhichintan2134@gmail.com", extracted={})
        Message.objects.create(ticket=t, direction=Message.DIRECTION_INBOUND,
                               from_email="dabhichintan2134@gmail.com",
                               headers={"From": self.SENDER})
        self.assertEqual(_customer_name(t), "Unknown")
        self.assertNotEqual(_customer_name(t), "Chintan Dabhi")

    def test_ai_extracted_name_without_verification_is_ignored(self):
        # An AI-parsed name (no shopify_verified source) is NOT trusted -> Unknown.
        from apps.integrations.care_panel_store import _customer_name
        t = Ticket.objects.create(organization=self.org, brand=self.brand, mailbox=self.mailbox,
                                  customer_email="x@y.com",
                                  extracted={"customer_name": "Some Sender"})
        self.assertEqual(_customer_name(t), "Unknown")


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class MobileVerificationE2ETests(VerificationFlowTests):
    """End-to-end: a customer who replies with their mobile (in any common format) must
    verify against Shopify and get a ticket -- never 'could not verify the provided info'."""

    PHONE = "7004810519"

    def _shop(self):
        order = {"order_id": "262339239", "shipped": True, "customer_name": "Raneesh K"}
        return {"shopify": FakeShopify(orders={"262339239": order},
                                       by_phone={self.PHONE: [order]}),
                "shipping": None, "gokwik": None}

    def _invoice_reply(self, reply_body, n):
        # Order-related (refund) flow -> global Shopify verification by the replied mobile.
        cat7, _ = Category.objects.get_or_create(
            brand=self.brand, code="7", defaults={"name": "Return, Refund & Replacement"})
        self._run(
            eml(subject="refund request", body="please refund my order",
                message_id=f"<a{n}@x>"),
            eml(subject="Re: refund request", body=reply_body, message_id=f"<b{n}@x>",
                in_reply_to=f"<a{n}@x>", references=f"<a{n}@x>"),
            clients=self._shop(), classify=self._classify("7. Return, Refund & Replacement", cat7))

    def test_all_mobile_reply_formats_verify(self):
        formats = ["mobile : 7004810519", "mobile:7004810519", "mobile number:7004810519",
                   "my mobile is 7004810519", "phone 7004810519", "7004810519"]
        for i, body in enumerate(formats):
            with self.subTest(body=body):
                Ticket.objects.all().delete()
                PendingConversation.objects.all().delete()
                self.mailbox.imap_last_uid = 0          # reset watermark per iteration
                self.mailbox.save(update_fields=["imap_last_uid"])
                self._invoice_reply(body, i)
                self.assertEqual(Ticket.objects.count(), 1,
                                 f"{body!r} did not verify -> no ticket")
                self.assertEqual(Ticket.objects.get().extracted.get("phone"), self.PHONE)

    def test_mobile_reply_verifies_and_creates_ticket(self):
        # A reply carrying the mobile must verify against Shopify -> ticket (not stuck on
        # "could not verify").
        self._invoice_reply("mobile : 7004810519", 99)
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Ticket.objects.get().extracted.get("phone"), self.PHONE)


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class GlobalVerificationRuleTests(VerificationFlowTests):
    """GLOBAL RULE: ANY order-related issue (category 1-8) must be verified (order / mobile /
    email, OR-based) before a ticket is created. Non-order categories (9-16) create
    immediately. Verified tickets use the Shopify order owner's name, never the sender."""

    PHONE = "6005911305"
    SHOPNAME = "Verified Owner"

    def setUp(self):
        super().setUp()
        self.cat7 = Category.objects.create(brand=self.brand, code="7",
                                            name="Return, Refund & Replacement")
        self.cat9 = Category.objects.create(brand=self.brand, code="9",
                                            name="Product Information & Inquiry")

    def _shop(self, name="Verified Owner"):
        order = {"order_id": "262339239", "shipped": True, "customer_name": name}
        return {"shopify": FakeShopify(orders={"262339239": order},
                                       by_phone={self.PHONE: [order]},
                                       by_email={"owner@shop.com": [order]}),
                "shipping": None, "gokwik": None}

    def _classify_for(self, category, cat_ref, summary="refund request"):
        return lambda b, m: ClassificationResult(
            category=category, sub_topic="", confidence=0.9,
            extracted={"customer_name": "Sender Name"}, sentiment="neutral", language="en",
            is_support_request=True, issue_summary=summary, requires_evidence=False,
            requires_agent=False, category_ref=cat_ref, sub_topic_ref=None)

    def _name(self, t):
        from apps.integrations.care_panel_store import _customer_name
        return _customer_name(t)

    # 1) order-related + no identifier -> BLOCKED (no ticket, verification request sent).
    def test_order_related_no_identifier_blocks_ticket(self):
        self._run(eml(subject="refund", body="please refund my order", message_id="<a@x>"),
                  clients=self._shop(),
                  classify=self._classify_for("7. Return, Refund & Replacement", self.cat7))
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertEqual(PendingConversation.objects.count(), 1)
        self.assertIn("could not verify", " ".join(s["body"] for s in self.sent).lower())

    # 2) order-related, verified by ORDER on the first email -> ticket created immediately.
    def test_order_related_verified_order_creates_ticket(self):
        self._run(eml(subject="refund", body="refund for order 262339239", message_id="<a@x>"),
                  clients=self._shop(),
                  classify=self._classify_for("7. Return, Refund & Replacement", self.cat7))
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(self._name(Ticket.objects.get()), self.SHOPNAME)

    # 3) order-related, verified by MOBILE on a reply -> ticket; name from Shopify, not sender.
    def test_order_related_verified_mobile_on_reply_creates_ticket(self):
        self._run(
            eml(subject="refund", body="please refund my order", message_id="<a@x>"),
            eml(subject="Re: refund", body=f"my mobile is {self.PHONE}", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>"),
            clients=self._shop(),
            classify=self._classify_for("7. Return, Refund & Replacement", self.cat7))
        self.assertEqual(Ticket.objects.count(), 1)
        t = Ticket.objects.get()
        self.assertEqual(self._name(t), self.SHOPNAME)
        self.assertNotEqual(self._name(t), "Sender Name")

    # 4) OR-logic: wrong order + correct EMAIL still verifies.
    def test_or_logic_email_verifies_when_order_wrong(self):
        self._run(
            eml(subject="refund", body="refund order 000000 email owner@shop.com",
                message_id="<a@x>"),
            clients=self._shop(),
            classify=self._classify_for("7. Return, Refund & Replacement", self.cat7))
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(self._name(Ticket.objects.get()), self.SHOPNAME)

    # 5) verification failure (wrong everything) never creates a ticket.
    def test_verification_failure_never_creates_ticket(self):
        self._run(
            eml(subject="refund", body="refund order 999999 mobile 9999999999", message_id="<a@x>"),
            clients=self._shop(),
            classify=self._classify_for("7. Return, Refund & Replacement", self.cat7))
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertEqual(PendingConversation.objects.count(), 1)

    # 6) NON-order category (9) creates a ticket immediately, no verification.
    def test_non_order_creates_ticket_immediately(self):
        self._run(eml(subject="product question", body="do you sell blue widgets?",
                      message_id="<a@x>"),
                  clients=self._shop(),
                  classify=self._classify_for("9. Product Information & Inquiry", self.cat9,
                                              summary="product question"))
        self.assertEqual(Ticket.objects.count(), 1)

    def test_is_order_related_helper(self):
        from types import SimpleNamespace
        for code, expected in [("1", True), ("3", True), ("8", True), ("9", False),
                               ("11", False), ("16", False), ("", False)]:
            ref = SimpleNamespace(code=code) if code else None
            r = SimpleNamespace(category=(f"{code}. X" if code else "Uncategorized"),
                                category_ref=ref, sub_topic="", issue_summary="")
            self.assertEqual(service._is_order_related(r), expected, f"code={code!r}")


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class OrderOwnerAlwaysWinsTests(VerificationFlowTests):
    """ORDER OWNER ALWAYS WINS: when a Shopify order is verified, the ticket customer identity
    (name/email/phone) is the ORDER OWNER's, never the sender. The sender is kept separately
    (sender_name/sender_email) for conversation history + reply routing only."""

    def test_stamp_uses_order_owner_and_keeps_sender_separate(self):
        ex = service._capture_sender_identity(
            {}, {"from_email": "dabhichintan2134@gmail.com", "from_name": "Chintan Dabhi"})
        ex = service._stamp_verified_customer(ex, {
            "order_id": "#U2425-10-486324", "customer_name": "vinod KR",
            "customer_phone": "+919847505805", "customer_email": "vinodhp07@gmail.com"})
        # Order owner wins for the ticket customer identity.
        self.assertEqual(ex["customer_name"], "vinod KR")
        self.assertEqual(ex["customer_email"], "vinodhp07@gmail.com")
        self.assertEqual(ex["phone"], "+919847505805")
        self.assertEqual(ex["customer_name_source"], "shopify_verified")
        # Sender preserved separately, NEVER as the customer identity.
        self.assertEqual(ex["sender_name"], "Chintan Dabhi")
        self.assertEqual(ex["sender_email"], "dabhichintan2134@gmail.com")

    def test_care_panel_payload_uses_order_owner_email(self):
        from apps.integrations import care_panel_store
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="dabhichintan2134@gmail.com",   # the SENDER (routing)
            subject="missing my order",
            extracted={"customer_name": "vinod KR", "customer_name_source": "shopify_verified",
                       "customer_email": "vinodhp07@gmail.com", "phone": "+919847505805",
                       "order_id": "486324", "sender_email": "dabhichintan2134@gmail.com",
                       "sender_name": "Chintan Dabhi"})
        p = care_panel_store._payload(t)
        self.assertEqual(p["name"], "vinod KR")
        self.assertEqual(p["email"], "vinodhp07@gmail.com")   # owner, NOT sender
        self.assertEqual(p["phone"], "9847505805")            # 10-digit owner phone

    def test_serializer_shows_owner_as_customer_sender_separate(self):
        from apps.tickets.serializers import TicketDetailSerializer
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="dabhichintan2134@gmail.com",   # the SENDER (reply routing)
            subject="missing my order",
            extracted={"customer_name": "vinod KR", "customer_name_source": "shopify_verified",
                       "customer_email": "vinodhp07@gmail.com", "phone": "+919847505805",
                       "sender_email": "dabhichintan2134@gmail.com", "sender_name": "Chintan Dabhi"})
        data = TicketDetailSerializer(t).data
        self.assertEqual(data["customer_name"], "vinod KR")
        self.assertEqual(data["customer_email"], "vinodhp07@gmail.com")   # owner shown
        self.assertEqual(data["customer_phone"], "+919847505805")
        self.assertEqual(data["sender_email"], "dabhichintan2134@gmail.com")   # routing
        self.assertEqual(data["sender_name"], "Chintan Dabhi")
        # The model field used for reply routing stays the SENDER.
        self.assertEqual(t.customer_email, "dabhichintan2134@gmail.com")

    def test_unverified_ticket_customer_name_is_unknown_not_sender(self):
        from apps.tickets.serializers import TicketDetailSerializer
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="someone@x.com", subject="hi",
            extracted={"sender_email": "someone@x.com", "sender_name": "Some One"})
        data = TicketDetailSerializer(t).data
        self.assertEqual(data["customer_name"], "Unknown")   # never the sender name
        self.assertEqual(data["sender_name"], "Some One")


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class VerificationLoopFixTests(VerificationFlowTests):
    """The 'could not verify' loop: a corrected mobile in a LATER reply must be verified
    (not the stale first one), and a customer is never trapped -- after MAX_VERIFY_ATTEMPTS
    the ticket is created (flagged unverified) instead of looping forever."""

    GOOD = "7987139394"     # matches a Shopify order
    BAD = "9895798462"      # no Shopify match

    def setUp(self):
        super().setUp()
        self.cat7 = Category.objects.create(brand=self.brand, code="7",
                                            name="Return, Refund & Replacement")

    def _shop(self):
        order = {"order_id": "262339239", "shipped": True, "customer_name": "Zehaan Khan",
                 "customer_phone": "7987139394"}
        return {"shopify": FakeShopify(orders={"262339239": order},
                                       by_phone={self.GOOD: [order]}),
                "shipping": None, "gokwik": None}

    def _classify(self):
        return lambda b, m: ClassificationResult(
            category="7. Return, Refund & Replacement", sub_topic="", confidence=0.9,
            extracted={}, sentiment="neutral", language="en", is_support_request=True,
            issue_summary="refund status", requires_evidence=False, requires_agent=False,
            category_ref=self.cat7, sub_topic_ref=None)

    def test_corrected_mobile_in_later_reply_verifies(self):
        self._run(
            eml(subject="Refund status", body="when will I get my refund?", message_id="<a@x>"),
            eml(subject="Re: Refund status", body=f"mobile number : {self.BAD}",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>"),
            eml(subject="Re: Refund status", body=f"mobile number: {self.GOOD}",
                message_id="<a3@x>", in_reply_to="<a@x>", references="<a@x>"),
            clients=self._shop(), classify=self._classify())
        self.assertEqual(Ticket.objects.count(), 1)        # corrected mobile verified -> ticket
        from apps.integrations.care_panel_store import _customer_name
        self.assertEqual(_customer_name(Ticket.objects.get()), "Zehaan Khan")

    def test_repeated_bad_mobile_escalates_not_loops(self):
        self._run(
            eml(subject="Refund status", body="refund please", message_id="<a@x>"),
            eml(subject="Re: Refund status", body=f"mobile {self.BAD}", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>"),
            eml(subject="Re: Refund status", body=f"mobile {self.BAD}", message_id="<a3@x>",
                in_reply_to="<a@x>", references="<a@x>"),
            clients=self._shop(), classify=self._classify())
        # MAX_VERIFY_ATTEMPTS reached -> ticket created (flagged unverified), no infinite loop.
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Ticket.objects.get().extracted.get("verify_unconfirmed"), "not_found")


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class VerificationFirstTicketTests(VerificationFlowTests):
    """VERIFICATION-FIRST: every ticket-creating category must verify the customer (order /
    mobile / email) before a ticket is created. Unverified -> 'could not verify', NO ticket.
    Evidence categories additionally require the screenshot/proof before the ticket."""

    PHONE = "9876543210"

    def setUp(self):
        super().setUp()
        self.cat15 = Category.objects.create(brand=self.brand, code="15",
                                             name="App & Website Technical Issues")
        self.cat3 = Category.objects.create(brand=self.brand, code="3",
                                            name="Delivery Issues (Post-Delivery)")

    def _shop(self):
        order = {"order_id": "262339239", "shipped": True, "customer_name": "Verified Owner",
                 "customer_phone": self.PHONE}
        return {"shopify": FakeShopify(orders={"262339239": order},
                                       by_phone={self.PHONE: [order]}),
                "shipping": None, "gokwik": None}

    def _classify(self, category, cat_ref, summary="issue", requires_evidence=False):
        return lambda b, m: ClassificationResult(
            category=category, sub_topic="", confidence=0.9, extracted={}, sentiment="neutral",
            language="en", is_support_request=True, issue_summary=summary,
            requires_evidence=requires_evidence, requires_agent=False, category_ref=cat_ref,
            sub_topic_ref=None)

    # 1) Verified customer -> ticket created. (Payment is a generic verify-first ticket
    #    category -- cat 15 now has its OWN guided flow, see tests_guided.)
    def test_verified_customer_creates_ticket(self):
        self._run(eml(subject="payment", body=f"I was charged twice. mobile {self.PHONE}",
                      message_id="<a@x>"),
                  clients=self._shop(),
                  classify=self._classify("8. Payment & Invoice", self.cat8, "charged twice"))
        self.assertEqual(Ticket.objects.count(), 1)
        from apps.integrations.care_panel_store import _customer_name
        self.assertEqual(_customer_name(Ticket.objects.get()), "Verified Owner")

    # 2) Unverified customer -> verification email sent, NO ticket.
    def test_unverified_customer_blocked_no_ticket(self):
        self._run(eml(subject="payment", body="I was charged twice", message_id="<a@x>"),
                  clients=self._shop(),
                  classify=self._classify("8. Payment & Invoice", self.cat8, "charged twice"))
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertEqual(PendingConversation.objects.count(), 1)
        self.assertIn("could not verify", self._last()["body"].lower())

    # 3) Verified but missing screenshot/proof -> ticket NOT created (evidence category).
    def test_verified_missing_screenshot_no_ticket(self):
        self._run(eml(subject="damaged", body=f"item damaged. mobile {self.PHONE}",
                      message_id="<a@x>"),
                  clients=self._shop(),
                  classify=self._classify("3. Delivery Issues (Post-Delivery)", self.cat3,
                                          "item is damaged", requires_evidence=True))
        self.assertEqual(Ticket.objects.count(), 0)               # verified, but no proof yet
        # Damaged now asks via the EV_DAMAGED template (unboxing video + clear images).
        self.assertIn("video", self._last()["body"].lower())      # asked for the proof

    # 4) Verified + screenshot/proof -> ticket created.
    def test_verified_with_proof_creates_ticket(self):
        self._run(
            eml(subject="damaged", body=f"item damaged. mobile {self.PHONE}", message_id="<a@x>"),
            # Damaged requires BOTH a photo AND a video -> supply both.
            eml(subject="Re: damaged", body="here is the photo and video", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True, video=True),
            clients=self._shop(),
            classify=self._classify("3. Delivery Issues (Post-Delivery)", self.cat3,
                                    "item is damaged", requires_evidence=True))
        self.assertEqual(Ticket.objects.count(), 1)               # verified + proof -> ticket


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class VerificationFirstAutoReplyTests(VerificationFlowTests):
    """VERIFICATION-FIRST for AUTO-REPLY categories (offers / item-or-GST edits / delete
    account / data privacy): verify the customer, THEN auto-reply -- NO ticket. Pure business
    inquiries and general info skip verification."""

    PHONE = "9876543210"

    def setUp(self):
        super().setUp()
        from apps.taxonomy.models import Rule, SubTopic
        self.cat10 = Category.objects.create(brand=self.brand, code="10",
                                             name="Offers, Discounts & Loyalty")
        self.cat9 = Category.objects.create(brand=self.brand, code="9",
                                            name="Product Information & Inquiry")
        self.offers_sub = SubTopic.objects.create(category=self.cat10, code="10.1",
                                                  name="Offer Query")
        Rule.objects.create(sub_topic=self.offers_sub, condition="Always",
                            then_response="Here are our current offers.",
                            action=Rule.ACTION_INFO_ONLY)

    def _shop(self):
        order = {"order_id": "262339239", "shipped": True, "customer_name": "Verified Owner",
                 "customer_phone": self.PHONE}
        return {"shopify": FakeShopify(orders={"262339239": order},
                                       by_phone={self.PHONE: [order]}),
                "shipping": None, "gokwik": None}

    def _classify(self, category, cat_ref, summary="query", sub=None):
        return lambda b, m: ClassificationResult(
            category=category, sub_topic=(sub.name if sub else ""), confidence=0.9, extracted={},
            sentiment="neutral", language="en", is_support_request=True, issue_summary=summary,
            requires_evidence=False, requires_agent=False, category_ref=cat_ref,
            sub_topic_ref=sub)

    def test_offers_general_auto_reply_no_verification_no_ticket(self):
        # Offers are now an IMMEDIATE auto-reply (Ongoing Offers & Sales) -- no verification,
        # no ticket, never 'could not verify' / a delivery issue.
        self._run(eml(subject="offers", body="what is the ongoing offer?", message_id="<a@x>"),
                  clients=self._shop(),
                  classify=self._classify("10. Offers, Discounts & Loyalty", self.cat10,
                                          "offer query"))
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertNotIn("could not verify", self._last()["body"].lower())
        self.assertIn("Please let us know the offer", self._last()["body"])

    def test_offers_problem_asks_for_screenshot_no_ticket(self):
        self._run(eml(subject="discount", body="discount not applied", message_id="<a@x>"),
                  clients=self._shop(),
                  classify=self._classify("10. Offers, Discounts & Loyalty", self.cat10,
                                          "discount problem"))
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertIn("Clear screenshot of the discount problem", self._last()["body"])

    def test_general_info_skips_verification(self):
        # Product info (cat 9) is general/pre-sale -> NOT verified, auto-answered.
        self._run(eml(subject="product", body="do you sell water bottles?", message_id="<a@x>"),
                  clients=self._shop(),
                  classify=self._classify("9. Product Information & Inquiry", self.cat9,
                                          "product question"))
        self.assertEqual(PendingConversation.objects.count(), 0)   # never blocked for verify
        self.assertNotIn("could not verify", (self._last() or {}).get("body", "").lower())

    def test_inquiry_skips_verification(self):
        # Pure business inquiry -> Inquiry workflow (no order verification).
        from apps.tickets.models import Inquiry
        self._run(eml(subject="dropshipping", body="I want to start dropshipping",
                      message_id="<a@x>"),
                  clients=self._shop(),
                  classify=self._classify("11. Wholesale / Bulk Purchase (B2B)", self.cat11,
                                          "inquiry"))
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertNotIn("could not verify", self._last()["body"].lower())
        self.assertIn("dropshipping program", self._last()["body"].lower())
