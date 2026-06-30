"""
Order-cancellation flow (reported bug: "cancel my order. my mobile is 9907465210"
was routed into the DAMAGE evidence workflow and the phone read as an order id).

    python manage.py test apps.ingestion.tests_cancellation
"""

from django.test import TestCase, override_settings

from apps.ingestion import evidence, service
from apps.ingestion.tests_imap import FakeImap
from apps.ingestion.tests_smart import eml
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import PendingConversation, Ticket


class CancellationPolicyTests(TestCase):
    def test_is_cancellation(self):
        for t in ["I want to cancel my order", "please cancel order", "order cancellation",
                  "cancel my order. my mobile is 9907465210", "I wish to cancel"]:
            self.assertTrue(evidence.is_cancellation(t), t)
        for t in ["my product is damaged", "where is my order", "wrong item"]:
            self.assertFalse(evidence.is_cancellation(t), t)

    def test_cancellation_beats_damage_keyword(self):
        # "cancel my damaged order" -> CANCELLATION, NOT a photo/video workflow.
        self.assertEqual(
            evidence.evidence_level(text="I want to cancel my damaged order",
                                    issue_summary="damaged"),
            evidence.EV_NONE)

    def test_damage_still_requires_photo(self):
        self.assertEqual(evidence.evidence_level(text="my product is damaged",
                                                 issue_summary="damaged product"),
                         evidence.EV_PHOTO)


@override_settings(PUBLIC_BASE_URL="https://support.deodap.in")
class CancellationFlowTests(TestCase):
    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)

    def _provider(self, *, damaged=False):
        import json as _json
        # Even if the AI says DAMAGED, cancellation detection must win for cancel text.
        cat = ("3. Delivery Issues", "3.3 Damaged") if damaged else ("6. Order Cancellation", "6.1 Cancel")

        class FP:
            def generate(self, system, user):
                return _json.dumps({
                    "is_support_request": True, "category": cat[0], "sub_topic": cat[1],
                    "confidence": 0.9, "requires_evidence": damaged, "requires_agent": False,
                    "issue_summary": "damaged" if damaged else "cancel order",
                    "sentiment": "neutral", "extracted": {}})
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

    # --- Case 1: cancellation -------------------------------------------------------
    def test_cancel_intent_phone_extracted_no_evidence(self):
        # Provider deliberately returns DAMAGED to prove cancellation overrides it.
        self._run(self._provider(damaged=True), [
            eml(subject="cancel order",
                body="I want to cancel my order. My mobile number is 9907465210.",
                message_id="<a@x>")])
        self.assertEqual(Ticket.objects.count(), 0)                # no ticket yet
        p = PendingConversation.objects.get()
        self.assertEqual(p.extracted.get("intent"), "ORDER_CANCELLATION")  # intent
        self.assertEqual(p.phone, "9907465210")                    # phone extracted
        self.assertEqual(p.order_id, "")                           # phone NOT used as order
        self.assertFalse(p.requires_evidence)                      # no evidence
        self.assertNotEqual(p.status, "waiting_for_video")         # NOT the damage path
        self.assertEqual(_pending_level(p), evidence.EV_NONE)      # never asks for photo/video
        self.assertGreaterEqual(p.evidence_requests, 1)            # M_CANCEL_LOOKUP sent

    def test_cancel_with_order_in_reply_creates_ticket(self):
        self._run(self._provider(damaged=True), [
            eml(subject="cancel order", body="I want to cancel my order, mobile 9907465210",
                message_id="<a@x>"),
            eml(subject="Re: cancel order", body="my order id is 9027510",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>")])
        self.assertEqual(Ticket.objects.count(), 1)                # created on the order reply
        self.assertEqual(Ticket.objects.get().extracted.get("order_id"), "9027510")

    # --- Case 2: damage still works -------------------------------------------------
    def test_damage_still_asks_for_photo(self):
        self._run(self._provider(damaged=True), [
            eml(subject="my product is damage",
                body="My product is damaged, my mobile is 8765321519", message_id="<a@x>")])
        self.assertEqual(Ticket.objects.count(), 0)
        p = PendingConversation.objects.get()
        self.assertNotEqual(p.extracted.get("intent"), "ORDER_CANCELLATION")
        self.assertEqual(_pending_level(p), evidence.EV_PHOTO)      # photo (M2P)
        self.assertEqual(p.status, "awaiting_evidence")


def _pending_level(p):
    return service._pending_evidence_level(p)
