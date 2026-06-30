"""
Website/App (cat 15) + Account (cat 14) guided sub-topic flows: AUTO-VERIFY from the first
message -> consolidated single-reply collect -> ticket (or guided auto-reply).

    python manage.py test apps.ingestion.tests_guided
"""
from django.test import override_settings

from apps.classifier.service import ClassificationResult
from apps.taxonomy.models import Category
from apps.tickets.models import PendingConversation, Ticket

from apps.ingestion.tests_smart import eml
from apps.ingestion.tests_verification import FakeShopify, VerificationFlowTests


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class GuidedFlowTests(VerificationFlowTests):
    PHONE = "9895798462"

    def setUp(self):
        super().setUp()
        self.cat15 = Category.objects.create(brand=self.brand, code="15",
                                             name="App & Website Technical Issues")
        self.cat14 = Category.objects.create(brand=self.brand, code="14",
                                             name="Account & Security")
        self.cat2 = Category.objects.create(brand=self.brand, code="2",
                                            name="Delivery Address & Customer Info Changes")
        self.cat5 = Category.objects.create(brand=self.brand, code="5",
                                            name="Order Placement & Modification")

    def _shop(self):
        order = {"order_id": "262339239", "shipped": True, "customer_name": "Verified Owner",
                 "customer_phone": self.PHONE}
        return {"shopify": FakeShopify(orders={"262339239": order},
                                       by_phone={self.PHONE: [order]}),
                "shipping": None, "gokwik": None}

    def _cls(self, code, name, sub):
        cat = {"15": self.cat15, "14": self.cat14, "2": self.cat2,
               "5": self.cat5}.get(code, self.cat15)
        return lambda b, m: ClassificationResult(
            category=f"{code}. {cat.name}", sub_topic=sub, confidence=0.9, extracted={},
            sentiment="neutral", language="en", is_support_request=True, issue_summary=name,
            requires_evidence=False, requires_agent=False, category_ref=cat, sub_topic_ref=None)

    def _bodies(self):
        return "\n".join(s["body"] for s in self.sent)

    # --- AUTO-VERIFICATION ------------------------------------------------------------------
    def test_app_crash_autoverify_then_single_collect_ticket(self):
        # First email ALREADY carries the mobile -> auto-verified, never asked to verify again.
        c = self._cls("15", "App Crashing / Not Loading", "App Crashing / Not Loading")
        self._run(
            eml(subject="App Crashing", body=f"App Crashing Mobile Number: {self.PHONE}",
                message_id="<a1@x>"),
            eml(subject="re", body="Issue: App crashes after login.", message_id="<a2@x>",
                image=True, video=True, in_reply_to="<a1@x>", references="<a1@x>"),
            classify=c, clients=self._shop())
        # First bot reply = "Verification successful." + the ONE consolidated detail request.
        self.assertIn("Verification successful", self.sent[0]["body"])
        self.assertIn("Screenshot", self.sent[0]["body"])
        self.assertIn("Video", self.sent[0]["body"])
        self.assertNotIn("any ONE", self.sent[0]["body"])         # never re-asked to verify
        self.assertEqual(Ticket.objects.count(), 1)
        t = Ticket.objects.get()
        self.assertTrue(t.attachments.filter(content_type__startswith="image/").exists())
        self.assertTrue(t.attachments.filter(content_type__startswith="video/").exists())

    def test_no_identifier_asks_then_verifies(self):
        c = self._cls("15", "App Crashing", "App Crashing / Not Loading")
        self._run(
            eml(subject="App", body="app crashing", message_id="<b1@x>"),          # no id
            eml(subject="re", body=self.PHONE, message_id="<b2@x>",
                in_reply_to="<b1@x>", references="<b1@x>"),                          # id now
            eml(subject="re", body="crashes", message_id="<b3@x>", image=True, video=True,
                in_reply_to="<b1@x>", references="<b1@x>"),
            classify=c, clients=self._shop())
        self.assertIn("any ONE", self.sent[0]["body"])             # asked because none found
        self.assertIn("Verification successful", self._bodies())   # then verified on the reply
        self.assertEqual(Ticket.objects.count(), 1)

    def test_verification_fails_message(self):
        c = self._cls("15", "App Crashing", "App Crashing / Not Loading")
        self._run(
            eml(subject="App", body="app crashing mobile 9999999999", message_id="<v1@x>"),
            classify=c, clients=self._shop())                       # 9999999999 not in shop
        self.assertIn("could not verify", self.sent[0]["body"].lower())
        self.assertEqual(Ticket.objects.count(), 0)

    def test_reasks_only_for_missing_video(self):
        c = self._cls("15", "App Crashing", "App Crashing / Not Loading")
        self._run(
            eml(subject="App", body=f"app crashing mobile {self.PHONE}", message_id="<m1@x>"),
            eml(subject="re", body="here", message_id="<m2@x>", image=True,       # photo only
                in_reply_to="<m1@x>", references="<m1@x>"),
            classify=c, clients=self._shop())
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertIn("Video", self.sent[-1]["body"])
        self.assertNotIn("Screenshot", self.sent[-1]["body"])

    # --- Ongoing Offers & Sales (auto-reply, no ticket) -------------------------------------
    def test_offers_general_auto_reply_no_ticket(self):
        c = self._cls("3", "Other Issue", "Other Issue")        # AI mis-classified as delivery
        self._run(eml(subject="offers", body="Any current offers?", message_id="<o1@x>"),
                  classify=c, clients=self._shop())
        self.assertEqual(Ticket.objects.count(), 0)             # NO ticket
        b = self._bodies()
        self.assertIn("Please let us know the offer", b)
        self.assertNotIn("Other Delivery Related Issue", b)
        self.assertNotIn("already open", b.lower())

    def test_offers_problem_asks_for_screenshot(self):
        c = self._cls("3", "Other Issue", "Other Issue")
        self._run(eml(subject="coupon", body="Coupon not applying", message_id="<o2@x>"),
                  classify=c, clients=self._shop())
        self.assertEqual(Ticket.objects.count(), 0)
        b = self._bodies()
        self.assertIn("Clear screenshot of the discount problem", b)
        self.assertIn("Offer/Coupon name", b)

    # --- TICKET CATEGORIES ------------------------------------------------------------------
    def test_cart_not_saving_ticket(self):
        c = self._cls("15", "Cart Not Saving Items", "Cart Not Saving Items")
        self._run(
            eml(subject="cart", body=f"cart not saving items mobile {self.PHONE}",
                message_id="<k1@x>"),
            eml(subject="re", body="items disappear", message_id="<k2@x>", image=True,
                video=True, in_reply_to="<k1@x>", references="<k1@x>"),
            classify=c, clients=self._shop())
        self.assertEqual(Ticket.objects.count(), 1)

    def test_checkout_is_ticket(self):
        c = self._cls("15", "Checkout Page Not Load", "Checkout Page Not Load")
        self._run(
            eml(subject="checkout", body=f"checkout page not load mobile {self.PHONE}",
                message_id="<c1@x>"),
            eml(subject="re", body="cannot pay", message_id="<c2@x>", image=True, video=True,
                in_reply_to="<c1@x>", references="<c1@x>"),
            classify=c, clients=self._shop())
        self.assertIn("Verification successful", self.sent[0]["body"])
        self.assertEqual(Ticket.objects.count(), 1)

    def test_update_contact_ticket(self):
        c = self._cls("14", "Update Phone / Email", "Update Phone / Email")
        self._run(
            eml(subject="update", body=f"update phone email mobile {self.PHONE}",
                message_id="<d1@x>"),
            eml(subject="re", body="New mobile 1234567890 new email example123@gmail.com",
                message_id="<d2@x>", in_reply_to="<d1@x>", references="<d1@x>"),
            classify=c, clients=self._shop())
        self.assertIn("New Mobile Number", self.sent[0]["body"])
        self.assertEqual(Ticket.objects.count(), 1)
        gd = (Ticket.objects.get().extracted or {}).get("guided_data") or {}
        self.assertEqual(gd.get("new_phone"), "1234567890")
        self.assertEqual(gd.get("new_email"), "example123@gmail.com")

    def test_otp_ticket(self):
        c = self._cls("14", "OTP / Notifications Not Received",
                      "OTP / Notifications Not Received")
        self._run(
            eml(subject="otp", body=f"otp not received mobile {self.PHONE}", message_id="<g1@x>"),
            eml(subject="re", body="Email abc@gmail.com Mobile 9876543210", message_id="<g2@x>",
                in_reply_to="<g1@x>", references="<g1@x>"),
            classify=c, clients=self._shop())
        self.assertEqual(Ticket.objects.count(), 1)

    # --- AUTO-REPLY CATEGORIES --------------------------------------------------------------
    def test_delete_account_auto_reply(self):
        c = self._cls("14", "Delete Account", "Delete Account")
        self._run(
            eml(subject="delete", body=f"delete account mobile {self.PHONE}", message_id="<e1@x>"),
            eml(subject="re", body="Email: abc@gmail.com Mobile: 9876543210 Reason: not using",
                message_id="<e2@x>", in_reply_to="<e1@x>", references="<e1@x>"),
            classify=c, clients=self._shop())
        self.assertIn("Verification successful", self.sent[0]["body"])
        self.assertIn("Reason for Account Deletion", self.sent[0]["body"])
        self.assertIn("deletion request has been submitted", self.sent[-1]["body"].lower())
        self.assertEqual(Ticket.objects.count(), 0)

    def test_data_privacy_auto_reply(self):
        c = self._cls("14", "Data & Privacy Security", "Data & Privacy Security")
        self._run(
            eml(subject="privacy", body=f"data privacy security mobile {self.PHONE}",
                message_id="<f1@x>"),
            eml(subject="re", body="How is my payment information stored?", message_id="<f2@x>",
                in_reply_to="<f1@x>", references="<f1@x>"),
            classify=c, clients=self._shop())
        self.assertIn("describe your concern", self.sent[0]["body"].lower())
        self.assertIn("secure payment", self.sent[-1]["body"].lower())
        self.assertEqual(Ticket.objects.count(), 0)

    # --- Make Changes To Order (exact wording) -----------------------------------------------
    def test_update_address_phone_ticket(self):
        # Auto-verify -> "Address / Phone Update Request" (New Address + New Mobile) -> ticket
        # with the custom "Received" confirmation.
        c = self._cls("2", "Update Address / Phone", "Update Address / Phone")
        self._run(
            eml(subject="address", body=f"update my address mobile {self.PHONE}",
                message_id="<ad1@x>"),
            eml(subject="re", body="New address: 12 MG Road, Pune. New mobile: 9876543210",
                message_id="<ad2@x>", in_reply_to="<ad1@x>", references="<ad1@x>"),
            classify=c, clients=self._shop())
        self.assertEqual(self.sent[0]["subject"], "Address / Phone Update Request")
        self.assertIn("Complete New Address", self.sent[0]["body"])
        self.assertIn("New Mobile Number", self.sent[0]["body"])
        self.assertEqual(Ticket.objects.count(), 1)
        t = Ticket.objects.get()
        gd = (t.extracted or {}).get("guided_data") or {}
        self.assertEqual(gd.get("new_mobile"), "9876543210")
        self.assertTrue(gd.get("new_address"))
        # Custom ticket confirmation (not the generic M5) -- sent as an outbound message.
        from apps.tickets.models import Message
        conf = t.messages.filter(direction=Message.DIRECTION_OUTBOUND).order_by("created_at").last()
        self.assertEqual(conf.subject, "Address / Phone Update Request Received")
        self.assertIn("pincode changes may not be possible", conf.body_text)

    def test_update_address_rejects_invalid_mobile(self):
        c = self._cls("2", "Update Address / Phone", "Update Address / Phone")
        self._run(
            eml(subject="address", body=f"update address mobile {self.PHONE}",
                message_id="<iv1@x>"),
            eml(subject="re", body="New address: 5 Park St. New mobile: 1234567890",  # invalid
                message_id="<iv2@x>", in_reply_to="<iv1@x>", references="<iv1@x>"),
            classify=c, clients=self._shop())
        self.assertEqual(Ticket.objects.count(), 0)             # invalid mobile -> no ticket
        self.assertIn("New Mobile Number", self.sent[-1]["body"])  # re-asks for the mobile

    def test_add_items_immediate_auto_reply_no_ticket(self):
        from apps.tickets.models import PendingConversation
        # Immediate rejection -- NO confirm step, NO verification, NO ticket.
        c = self._cls("5", "Add / Update Items", "Add / Update Items")
        self._run(
            eml(subject="i want to add item", body="i want to add one item",
                message_id="<it1@x>"),
            classify=c, clients=self._shop())
        self.assertEqual(self.sent[0]["body"],
                         "Additional items cannot be added to an existing order. Please place a "
                         "new order for the required items.\n\nRegards,\nDeoDap Support Team")
        self.assertNotIn("Please confirm whether", self._bodies())   # no confirm step
        self.assertEqual(Ticket.objects.count(), 0)                  # NO ticket
        self.assertEqual(PendingConversation.objects.exclude(status="closed").count(), 0)

    def test_gst_update_auto_reply_no_ticket(self):
        c = self._cls("5", "Add / Update GST Details", "Add / Update GST Details")
        self._run(
            eml(subject="gst", body=f"please update gst number mobile {self.PHONE}",
                message_id="<g1@x>"),
            classify=c, clients=self._shop())
        self.assertEqual(self.sent[-1]["subject"], "GST Details Update Request")
        self.assertIn("GSTIN updates are not allowed", self.sent[-1]["body"])
        self.assertEqual(Ticket.objects.count(), 0)             # verify -> auto-reply, no ticket

    def test_mco_verification_fails_message(self):
        # GST still verifies first -> an unknown mobile gets the Verification Required email.
        c = self._cls("5", "Add / Update GST Details", "Add / Update GST Details")
        self._run(
            eml(subject="gst", body="update gst mobile 9999999999", message_id="<f1@x>"),
            classify=c, clients=self._shop())                   # 9999999999 not in shop
        self.assertEqual(self.sent[0]["subject"], "Verification Required")
        self.assertIn("could not verify your order details", self.sent[0]["body"].lower())
        self.assertEqual(Ticket.objects.count(), 0)


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class NoTicketSafetyGuardTests(VerificationFlowTests):
    """MANDATORY SAFETY CHECK: Add/Update Items & Add/Update GST must NEVER create a ticket --
    auto-reply only -- enforced at the policy AND the ticket-creation chokepoints."""

    def setUp(self):
        super().setUp()
        self.cat5 = Category.objects.create(brand=self.brand, code="5",
                                            name="Order Placement & Modification")

    def _ticket(self, sub_topic):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="x", category="5. Order Placement & Modification",
            sub_topic=sub_topic, extracted={"phone": "9876543210"})

    def test_policy_blocks_and_never_requires_ticket(self):
        from apps.decision import policy
        for sub in ("Add / Update Items", "Add / Update GST Details"):
            self.assertTrue(policy.blocks_ticket("", sub), sub)
            self.assertFalse(policy.requires_ticket("5", sub), sub)
        self.assertFalse(policy.blocks_ticket("", "Update Address / Phone"))  # ticket allowed

    def test_store_care_panel_blocked(self):
        # Even if forced, the Care Panel store is a no-op for these sub-topics.
        from apps.ingestion import service
        for sub in ("Add / Update Items", "Add / Update GST Details"):
            t = self._ticket(sub)
            with self.assertLogs("apps.ingestion.service", level="WARNING") as cm:
                service._store_care_panel(t)
            t.refresh_from_db()
            self.assertEqual((t.extracted or {}).get("care_panel_ticket_id"), None)
            self.assertTrue(any("Blocked ticket creation" in m for m in cm.output))

    def test_send_confirmation_created_blocked(self):
        from apps.ingestion import service
        from apps.tickets.models import Message
        t = self._ticket("Add / Update Items")
        self.assertIsNone(service.send_confirmation(t, "created"))
        self.assertFalse(t.messages.filter(direction=Message.DIRECTION_OUTBOUND).exists())

    def test_address_update_still_allowed(self):
        # Control: Update Address / Phone is NOT blocked.
        from apps.decision import policy
        self.assertFalse(policy.blocks_ticket("2", "Update Address / Phone"))
        self.assertTrue(policy.requires_ticket("2", "Update Address / Phone"))

    def test_finalize_skips_existing_ticket_lookup(self):
        # A blocked sub-topic reaching the normal pipeline must NOT run match_and_merge / send
        # the M6 'already open' email -- it sends the configured auto-reply and closes.
        from apps.classifier.service import ClassificationResult
        from apps.ingestion import service
        from apps.tickets.models import Message
        existing = self._ticket("Add / Update Items")        # an open ticket that COULD match
        existing.status = Ticket.STATUS_AWAITING_AGENT
        existing.extracted = {"order_id": "999"}
        existing.save()
        new_t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="add items", extracted={"order_id": "999"})
        Message.objects.create(ticket=new_t, direction=Message.DIRECTION_INBOUND,
                               from_email="b@x.com", subject="add items",
                               body_text="add one more item to my order")
        result = ClassificationResult(
            category="5. Order Placement & Modification", sub_topic="Add / Update Items",
            confidence=0.9, extracted={}, sentiment="neutral", language="en",
            is_support_request=True, issue_summary="add items", requires_evidence=False,
            requires_agent=False, category_ref=self.cat5, sub_topic_ref=None)
        out = service._finalize_new_ticket(new_t, result)
        body = "\n".join(out.messages.filter(direction=Message.DIRECTION_OUTBOUND)
                         .values_list("body_text", flat=True))
        self.assertIn("Additional items cannot be added", body)
        self.assertNotIn("already open", body.lower())        # NOT the M6 existing-ticket email
        self.assertEqual(out.status, Ticket.STATUS_AUTO_RESOLVED)
        self.assertEqual((out.extracted or {}).get("care_panel_ticket_id"), None)

    def test_match_and_merge_skipped_for_blocked(self):
        from apps.ingestion import service
        t = self._ticket("Add / Update GST Details")
        self.assertIsNone(service.match_and_merge(t))         # never looks up an existing ticket

    # --- Natural-phrasing intents (no exact sub-topic label) must still be no-ticket ----------
    PHONE = "9895798462"

    def _shop(self):
        order = {"order_id": "262339239", "customer_name": "Owner", "customer_phone": self.PHONE}
        return {"shopify": FakeShopify(orders={"262339239": order},
                                       by_phone={self.PHONE: [order]}),
                "shipping": None, "gokwik": None}

    def _cls5(self):
        from apps.classifier.service import ClassificationResult
        return lambda b, m: ClassificationResult(
            category="5. Order Placement & Modification", sub_topic="", confidence=0.9,
            extracted={}, sentiment="neutral", language="en", is_support_request=True,
            issue_summary="", requires_evidence=False, requires_agent=False,
            category_ref=self.cat5, sub_topic_ref=None)

    def test_add_one_more_item_natural_phrasing_no_ticket(self):
        self._run(
            eml(subject="Add one more item to my order",
                body=f"Add one more item to my order. Mobile number: {self.PHONE}",
                message_id="<n1@x>"),
            classify=self._cls5(), clients=self._shop())
        b = "\n".join(s["body"] for s in self.sent)
        self.assertEqual(Ticket.objects.count(), 0)               # NO ticket
        self.assertNotIn("already open", b.lower())               # NO existing-ticket email
        self.assertNotIn("Track the latest update", b)            # NO tracking link
        self.assertIn("Additional items cannot be added", b)      # immediate rejection reply

    def test_add_update_gst_natural_phrasing_no_ticket(self):
        self._run(
            eml(subject="Add and update GST details",
                body=f"Add and update GST details. Mobile number: {self.PHONE}",
                message_id="<n2@x>"),
            classify=self._cls5(), clients=self._shop())
        b = "\n".join(s["body"] for s in self.sent)
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertIn("GSTIN updates are not allowed", b)
        self.assertNotIn("already open", b.lower())
