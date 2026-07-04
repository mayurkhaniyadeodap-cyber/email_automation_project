"""
Tests for the category-first evidence policy + workflow:

  Defective / Missing / Wrong Item -> VIDEO mandatory (photo-only insufficient)
  Damaged                          -> PHOTO required (video optional)
  Tracking / Refund / Return / General -> NO evidence

    python manage.py test apps.ingestion.tests_evidence
"""

from django.test import TestCase, override_settings

from apps.ingestion import evidence, service
from apps.ingestion.tests_imap import FakeImap
from apps.ingestion.tests_smart import eml
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, SubTopic
from apps.tickets.models import PendingConversation, Ticket


class EvidencePolicyTests(TestCase):
    """The deterministic keyword policy (works even with no sub-topic mapped)."""

    def test_video_intents(self):
        for text in ["Defective item received", "my product is not working",
                     "missing item in my order", "wrong item delivered",
                     "I did not receive my order"]:
            self.assertEqual(evidence.policy_for_text(text), evidence.EV_VIDEO, text)

    def test_photo_intents(self):
        for text in ["damaged product", "arrived broken", "bad quality item",
                     "the box is cracked"]:
            self.assertEqual(evidence.policy_for_text(text), evidence.EV_PHOTO, text)

    def test_none_intents(self):
        for text in ["where is my order", "track my shipment", "I want a refund",
                     "how do I return this", "do you have this in blue"]:
            self.assertEqual(evidence.policy_for_text(text), evidence.EV_NONE, text)

    def test_db_video_flag_raises_level(self):
        # A sub-topic flagged requires_video forces VIDEO even for neutral text.
        class Sub:
            requires_video = True
            requires_evidence = True
            name = "Replacement"
        self.assertEqual(
            evidence.evidence_level(category="Returns", sub_topic_ref=Sub()),
            evidence.EV_VIDEO)

    def test_keyword_beats_missing_flags(self):
        # No DB flags, coarse category, but the issue text says "defective" -> VIDEO.
        self.assertEqual(
            evidence.evidence_level(category="7. Return, Refund & Replacement",
                                    issue_summary="defective item, not working"),
            evidence.EV_VIDEO)

    def test_ai_hint_is_photo_floor(self):
        self.assertEqual(
            evidence.evidence_level(category="9. Product Inquiry", ai_requires_evidence=True),
            evidence.EV_PHOTO)


class DeliveredItemSubtypeTests(TestCase):
    """Deterministic keyword -> Delivered-Item sub-type. 'damage' must NEVER be Missing Item."""

    def test_required_mappings(self):
        cases = {
            "My order is damaged": "Damaged Item",
            "My order is damage. I want to return it.": "Damaged Item",
            "Product is broken": "Damaged Item",
            "Received damaged parcel": "Damaged Item",
            "The box arrived cracked": "Damaged Item",
            "One item is missing": "Missing Item",
            "item not received in my order": "Missing Item",
            "Received wrong product": "Wrong Item",
            "different product received": "Wrong Item",
            "Product not working": "Defective Item",
            "the device is defective": "Defective Item",
            "quantity issue, received less quantity": "Quantity Issue",
        }
        for text, expected in cases.items():
            self.assertEqual(evidence.delivered_item_subtype(text), expected, text)

    def test_damage_never_missing(self):
        for text in ["My order is damage", "my order is damaged", "order damage return",
                     "physically damaged item"]:
            self.assertEqual(evidence.delivered_item_subtype(text), "Damaged Item", text)
            self.assertNotEqual(evidence.delivered_item_subtype(text), "Missing Item")

    def test_no_keyword_returns_none(self):
        self.assertIsNone(evidence.delivered_item_subtype("where is my order"))
        self.assertIsNone(evidence.delivered_item_subtype("I want a refund"))


class _Flow(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        from apps.brand_settings.models import BrandSettings
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)

    def _provider(self, category, sub_topic, issue, requires_evidence=True):
        import json as _json

        class FP:
            def generate(self, system, user):
                return _json.dumps({
                    "is_support_request": True, "category": category,
                    "sub_topic": sub_topic, "confidence": 0.9,
                    "requires_evidence": requires_evidence, "requires_agent": False,
                    "issue_summary": issue, "sentiment": "neutral", "extracted": {}})
        return FP()

    def _run(self, provider, emails):
        from apps.classifier import service as classifier
        orig = classifier.build_provider
        classifier.build_provider = lambda s: provider
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            classifier.build_provider = orig


class VideoMandatoryFlowTests(_Flow):
    def setUp(self):
        super().setUp()
        cat = Category.objects.create(brand=self.brand, code="7", name="Return, Refund & Replacement")
        SubTopic.objects.create(category=cat, code="7.1", name="Defective Item",
                                requires_evidence=True, requires_video=True,
                                mandatory_inputs=["order_id"])

    def test_no_attachment_waits_for_video(self):
        # Body carries an order id so the evidence-category verification gate is satisfied
        # (Shopify not configured here -> verify-soft proceeds) and we reach the video wait.
        self._run(self._provider("7. Return, Refund & Replacement", "7.1 Defective Item",
                                 "defective item not working"),
                  [eml(subject="Defective item", body="it doesn't work. order 123456",
                       message_id="<a@x>")])
        self.assertEqual(Ticket.objects.count(), 0)
        p = PendingConversation.objects.get()
        self.assertEqual(p.status, "waiting_for_video")

    def test_photo_only_still_waits_for_video(self):
        prov = self._provider("7. Return, Refund & Replacement", "7.1 Defective Item",
                              "defective item not working")
        self._run(prov, [
            eml(subject="Defective item", body="broke", message_id="<a@x>"),
            eml(subject="Re: Defective", body="photo", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True),
        ])
        self.assertEqual(Ticket.objects.count(), 0)              # photo NOT enough
        p = PendingConversation.objects.get()
        self.assertTrue(p.has_photo)
        self.assertFalse(p.has_video)
        self.assertEqual(p.status, "waiting_for_video")

    def test_video_proceeds_past_evidence(self):
        prov = self._provider("7. Return, Refund & Replacement", "7.1 Defective Item",
                              "defective item not working")
        self._run(prov, [
            eml(subject="Defective item", body="broke", message_id="<a@x>"),
            eml(subject="Re: Defective", body="video, order #12345", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", video=True),
        ])
        # video + order id present, phone not required in tests -> ticket created.
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertTrue(Ticket.objects.get().attachments.filter(
            content_type__startswith="video/").exists())


class DamagedPhotoFlowTests(_Flow):
    def setUp(self):
        super().setUp()
        cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        SubTopic.objects.create(category=cat, code="3.3", name="Damaged Item",
                                requires_evidence=True, requires_video=False,
                                mandatory_inputs=["order_id"])

    def test_no_attachment_asks_for_photo_not_video(self):
        self._run(self._provider("3. Delivery Issues", "3.3 Damaged Item", "damaged product"),
                  [eml(subject="Damaged item", body="arrived broken", message_id="<a@x>")])
        self.assertEqual(Ticket.objects.count(), 0)
        p = PendingConversation.objects.get()
        self.assertEqual(p.status, "awaiting_evidence")         # NOT waiting_for_video

    def test_photo_is_sufficient_creates_ticket(self):
        # NEW rule: Damaged requires BOTH a photo AND a video. A photo-only reply is no
        # longer enough -- it stays pending (waiting_for_video); the follow-up video reply
        # then supplies the second mandatory file and the ticket is created.
        prov = self._provider("3. Delivery Issues", "3.3 Damaged Item", "damaged product")
        self._run(prov, [
            eml(subject="Damaged item", body="broken", message_id="<a@x>"),
            eml(subject="Re: Damaged", body="photo, order #12345", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True),
        ])
        self.assertEqual(Ticket.objects.count(), 0)             # photo alone is NOT enough now
        self.assertEqual(PendingConversation.objects.get().status, "waiting_for_video")
        # Follow-up video reply -> both mandatory files present -> ticket created.
        self.mailbox.imap_last_uid = 0                          # let the follow-up UID through
        self.mailbox.save(update_fields=["imap_last_uid"])
        self._run(prov, [
            eml(subject="Re: Damaged", body="here is the video", message_id="<a3@x>",
                in_reply_to="<a@x>", references="<a@x>", video=True),
        ])
        self.assertEqual(Ticket.objects.count(), 1)             # photo + video -> ticket
        t = Ticket.objects.get()
        self.assertTrue(t.attachments.filter(content_type__startswith="image/").exists())
        self.assertTrue(t.attachments.filter(content_type__startswith="video/").exists())


class NoEvidenceFlowTests(_Flow):
    def setUp(self):
        super().setUp()
        cat = Category.objects.create(brand=self.brand, code="1", name="Shipment & Delivery Tracking")
        SubTopic.objects.create(category=cat, code="1.1", name="Shipment Status")

    def test_tracking_email_not_held_for_evidence(self):
        # Shipment Tracking never asks for photo/video and never creates a support ticket --
        # it does an order lookup (auto-reply). No video/photo hold is ever created.
        self._run(self._provider("1. Shipment & Delivery Tracking", "1.1 Shipment Status",
                                 "where is my order", requires_evidence=False),
                  [eml(subject="Where is my order", body="track please DD9999", message_id="<a@x>")])
        self.assertEqual(Ticket.objects.count(), 0)             # tracking is auto-reply, no ticket
        self.assertFalse(                                       # never an evidence/video hold
            PendingConversation.objects.filter(status="waiting_for_video").exists())


class TrackingFallbackTests(TestCase):
    """_ensure_tracking: keep a Care Panel link if present, else mint an internal one."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self, **extra):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="damaged", **extra)

    def test_no_care_panel_hash_yields_no_link(self):
        # No real Care Panel hash -> NO link (an internal hash on care.deodap.in 404s).
        # The internal tracking_hash is still recorded for our own /t portal.
        t = self._ticket()
        url = service._ensure_tracking(t)
        t.refresh_from_db()
        self.assertEqual(url, "")
        self.assertEqual(t.tracking_url, "")
        self.assertTrue(t.extracted.get("tracking_hash"))

    def test_real_care_panel_hash_yields_care_link(self):
        t = self._ticket(extracted={"care_panel_ticket_id": "RealHash99"})
        url = service._ensure_tracking(t)
        self.assertEqual(url, "https://care.deodap.in/t?id=RealHash99")
        self.assertNotIn("127.0.0.1", url)
        self.assertNotIn("localhost", url)
        t.refresh_from_db()
        self.assertFalse(t.extracted.get("internal_tracking"))   # real hash, not internal
        self.assertEqual(t.extracted.get("tracking_hash"), "RealHash99")

    def test_keeps_existing_care_panel_link(self):
        # A REAL Care Panel link (hash == care_panel_ticket_id) is kept untouched.
        t = self._ticket(tracking_url="https://care.deodap.in/t?id=REALhash99",
                         ticket_number="2606090601",
                         extracted={"care_panel_ticket_id": "REALhash99"})
        url = service._ensure_tracking(t)
        t.refresh_from_db()
        self.assertEqual(url, "https://care.deodap.in/t?id=REALhash99")  # NOT overwritten
        self.assertFalse(t.extracted.get("internal_tracking"))
        self.assertFalse(t.audit_log.filter(event="internal_tracking_generated").exists())

    def test_keeps_existing_care_panel_link_when_hash_only_in_url(self):
        # A real Care Panel URL should be preserved and its hash should be recorded.
        t = self._ticket(tracking_url="https://care.deodap.in/t?id=REALhash99")
        url = service._ensure_tracking(t)
        t.refresh_from_db()
        self.assertEqual(url, "https://care.deodap.in/t?id=REALhash99")
        self.assertEqual(t.tracking_url, "https://care.deodap.in/t?id=REALhash99")
        self.assertEqual(t.extracted.get("care_panel_ticket_id"), "REALhash99")
        self.assertEqual(t.extracted.get("tracking_hash"), "REALhash99")
        self.assertFalse(t.extracted.get("internal_tracking"))

    def test_internal_hash_on_care_panel_is_cleared(self):
        # A care.deodap.in link carrying an INTERNAL hash (no real care_panel_ticket_id)
        # 404s -> it must be cleared, not kept.
        t = self._ticket(tracking_url="https://care.deodap.in/t?id=djangoHash1",
                         extracted={"tracking_hash": "djangoHash1", "internal_tracking": True})
        url = service._ensure_tracking(t)
        self.assertEqual(url, "")

    def test_lan_link_is_cleared(self):
        # A legacy localhost/LAN link (no real hash) is cleared -- never emitted.
        t = self._ticket(tracking_url="http://192.168.1.2:8000/t?id=djangoHash1",
                         extracted={"tracking_hash": "djangoHash1", "internal_tracking": True})
        url = service._ensure_tracking(t)
        self.assertEqual(url, "")
        self.assertNotIn("192.168", url)

    def test_internal_link_is_deterministic(self):
        t = self._ticket()
        url1 = service._ensure_tracking(t)
        t.tracking_url = ""        # simulate a re-run
        t.save(update_fields=["tracking_url"])
        url2 = service._ensure_tracking(t)
        self.assertEqual(url1, url2)


class HasIdentifierTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _p(self, **kw):
        return PendingConversation.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox, **kw)

    def test_email_alone_is_an_identifier(self):
        self.assertTrue(service._has_identifier(self._p(customer_email="b@x.com")))

    def test_order_id_alone_is_an_identifier(self):
        self.assertTrue(service._has_identifier(self._p(order_id="DD9999")))

    def test_phone_alone_is_an_identifier(self):
        self.assertTrue(service._has_identifier(self._p(phone="9876543210")))

    def test_nothing_is_not_an_identifier(self):
        self.assertFalse(service._has_identifier(self._p()))


class NoOrderIdRequestTests(_Flow):
    """Guard: the system must NEVER send an 'Order ID' request email, and the M3
    template must not exist. Order id / phone are optional and never block a ticket."""

    def setUp(self):
        super().setUp()
        cat = Category.objects.create(brand=self.brand, code="3", name="Delivery Issues")
        SubTopic.objects.create(category=cat, code="3.3", name="Damaged",
                                requires_evidence=True, mandatory_inputs=["order_id"])

    def test_no_order_id_template_exists(self):
        from apps.ingestion import mails
        self.assertNotIn("M3", mails.MAILS)
        with self.assertRaises(KeyError):
            mails.render("M3", "en")

    def test_damaged_flow_sends_no_order_id_email(self):
        from apps.tickets.models import Message, Ticket
        prov = self._provider("3. Delivery Issues", "3.3 Damaged", "damaged product")
        self._run(prov, [
            eml(subject="damaged", body="broke, no order number at all", message_id="<a@x>"),
            # Damaged requires BOTH a photo AND a video -> supply both so the ticket is created.
            eml(subject="Re: damaged", body="here is the photo and video", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", image=True, video=True)])
        # Ticket created on the photo, with NO order id anywhere.
        self.assertEqual(Ticket.objects.count(), 1)
        # No outbound message may ask for an Order ID.
        for m in Message.objects.filter(direction=Message.DIRECTION_OUTBOUND):
            body = (m.body_text or "").lower()
            self.assertNotIn("order id", body, m.body_text)
            self.assertNotIn("your order number", body, m.body_text)


class EvidenceNoDoubleEmailTests(_Flow):
    """BUG: after a photo/video reply creates the ticket, the decision engine re-asks for
    evidence. Root cause: build_context didn't expose has_photo/has_unboxing_video, so the
    engine's 'Evidence present' rule + the no-re-ask guard never saw the evidence."""

    SEED_ASK = "unboxing video or a clear photo of the damage"   # the engine's AWAIT template

    def setUp(self):
        super().setUp()
        from apps.taxonomy.models import Rule
        self.cat = Category.objects.create(brand=self.brand, code="3",
                                           name="Delivery Issues (Post-Delivery)")
        self.sub = SubTopic.objects.create(category=self.cat, code="3.3", name="Damaged",
                                           requires_evidence=True, mandatory_inputs=["order_id"])
        Rule.objects.create(sub_topic=self.sub, position=1,
                            condition="No unboxing video / photo evidence present",
                            then_response="Sorry to hear that! To process your claim quickly, "
                                          "please reply with an unboxing video or a clear photo "
                                          "of the damage.",
                            action=Rule.ACTION_AWAIT_EVIDENCE)
        Rule.objects.create(sub_topic=self.sub, position=2, condition="Evidence present",
                            then_response="Complaint registered; routed to agent.",
                            action=Rule.ACTION_CREATE_TICKET)

    def _outbound(self):
        from apps.tickets.models import Message
        return "\n".join(Message.objects.filter(direction=Message.DIRECTION_OUTBOUND)
                         .values_list("body_text", flat=True))

    def _reply_creates_ticket_no_second_ask(self, **attach):
        prov = self._provider("3. Delivery Issues (Post-Delivery)", "3.3 Damaged",
                              "item is damaged")
        self._run(prov, [
            eml(subject="Damaged item", body="my item is damaged. order 123456",
                message_id="<a@x>"),
            eml(subject="Re: Damaged item", body="evidence attached", message_id="<a2@x>",
                in_reply_to="<a@x>", references="<a@x>", **attach),
        ])
        self.assertEqual(Ticket.objects.count(), 1)            # ticket created
        # the engine's evidence-request template must NEVER be sent after the ticket exists.
        self.assertEqual(self._outbound().count(self.SEED_ASK), 0)

    def test_photo_reply_creates_ticket_no_evidence_email(self):
        # Damaged now requires BOTH a photo AND a video before a ticket -> supply both.
        self._reply_creates_ticket_no_second_ask(image=True, video=True)

    def test_video_reply_creates_ticket_no_evidence_email(self):
        # Damaged now requires BOTH a photo AND a video before a ticket -> supply both.
        self._reply_creates_ticket_no_second_ask(image=True, video=True)

    def test_photo_and_video_reply_creates_ticket_no_evidence_email(self):
        self._reply_creates_ticket_no_second_ask(image=True, video=True)

    def test_build_context_exposes_evidence_flags(self):
        from apps.integrations.context import build_context
        t = Ticket.objects.create(organization=self.org, brand=self.brand, mailbox=self.mailbox,
                                  customer_email="b@x.com", extracted={"has_photo": True})
        facts = build_context(t, clients={"shopify": None, "shipping": None, "gokwik": None})
        self.assertTrue(facts.get("has_photo"))

    def _engine_ticket(self):
        # A damaged ticket with a MISSING mandatory order_id and NO has_photo flag in ctx --
        # exactly the production state where the missing-inputs gate sent the evidence email.
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox, customer_email="b@x.com",
            subject="Damaged", category="3. Delivery Issues (Post-Delivery)",
            sub_topic="3.3 Damaged", category_ref=self.cat, sub_topic_ref=self.sub,
            ai_confidence=0.95, classification_status=Ticket.CLS_CLASSIFIED,
            extracted={"requires_evidence": True})

    def test_engine_requests_evidence_when_no_attachment(self):
        from apps.decision import engine
        plan = engine.decide(self._engine_ticket(), context={})
        self.assertEqual(plan.action_code, "evidence_request")       # legit first ask

    def test_engine_skips_evidence_request_with_photo_attachment(self):
        from apps.decision import engine
        from apps.tickets.models import Attachment
        t = self._engine_ticket()
        Attachment.objects.create(ticket=t, filename="care panel.png", content_type="image/png")
        plan = engine.decide(t, context={})
        self.assertNotEqual(plan.action_code, "evidence_request")     # NOT re-asked
        self.assertNotIn(self.SEED_ASK, plan.reply_text or "")

    def test_engine_skips_evidence_request_with_video_attachment(self):
        from apps.decision import engine
        from apps.tickets.models import Attachment
        t = self._engine_ticket()
        Attachment.objects.create(ticket=t, filename="clip.mp4", content_type="video/mp4")
        plan = engine.decide(t, context={})
        self.assertNotEqual(plan.action_code, "evidence_request")
        self.assertNotIn(self.SEED_ASK, plan.reply_text or "")


class DeliveredNotReceivedTests(TestCase):
    """'Order Shown Delivered But Not Received' is a NON-DELIVERY dispute -- an unboxing
    video is impossible, so it must NEVER require photo/video evidence (the reported bug)."""

    def test_delivered_not_received_requires_no_evidence(self):
        from apps.ingestion import evidence as ev
        for t in ["Tracking shows delivered but I have not received the package",
                  "order shown delivered but not received",
                  "marked as delivered but package never received",
                  "status delivered but didn't receive it"]:
            with self.subTest(t=t):
                self.assertTrue(ev.is_delivered_not_received(t))
                self.assertIsNone(ev.delivered_item_subtype(t))   # NOT Missing Item
                lvl = ev.evidence_level(category="3. Delivery Issues (Post-Delivery)",
                                        sub_topic="Order Shown Delivered But Not Received",
                                        issue_summary=t, ai_requires_evidence=True)
                self.assertEqual(lvl, ev.EV_NONE)
                self.assertFalse(ev.requires_evidence(lvl))
                self.assertFalse(ev.requires_video(lvl))

    def test_item_condition_still_requires_evidence(self):
        # A damaged/wrong item mislabeled "not received" is still an evidence case.
        from apps.ingestion import evidence as ev
        self.assertFalse(ev.is_delivered_not_received("my order arrived damaged, shows delivered"))
        self.assertEqual(ev.delivered_item_subtype("my order arrived damaged, shows delivered"),
                         "Damaged Item")
        self.assertFalse(ev.is_delivered_not_received("delivered but wrong item received"))
        self.assertEqual(ev.delivered_item_subtype("delivered but wrong item received"),
                         "Wrong Item")

    def test_engine_does_not_require_evidence_for_delivered_not_received(self):
        from apps.organizations.models import Organization, Brand, Mailbox
        from apps.taxonomy.models import Category, SubTopic
        from apps.tickets.models import Ticket
        from apps.decision import engine
        org = Organization.objects.create(name="D")
        brand = Brand.objects.create(organization=org, name="B")
        Mailbox.objects.create(brand=brand, email_address="c@d.com")
        cat = Category.objects.create(brand=brand, code="3", name="Delivery Issues (Post-Delivery)")
        # Sub-topic with the DB evidence flag SET -> proves the engine override still wins.
        sub = SubTopic.objects.create(category=cat, code="3.9",
                                      name="Order Shown Delivered But Not Received",
                                      requires_evidence=True)
        t = Ticket.objects.create(
            organization=org, brand=brand, customer_email="b@x.com",
            subject="Tracking shows delivered but not received",
            issue_summary="Tracking shows delivered but I have not received the package",
            category="3. Delivery Issues (Post-Delivery)",
            sub_topic="Order Shown Delivered But Not Received",
            category_ref=cat, sub_topic_ref=sub)
        self.assertFalse(engine._evidence_required(t, sub, {"requires_evidence": True}))
