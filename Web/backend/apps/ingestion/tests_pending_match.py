"""
_find_pending must attach a new email to an existing pending ONLY by thread signal --
In-Reply-To / References / ticket reference -- never by sender email or sender+subject.
Two separate emails with the same sender AND same subject (different Message-IDs, no thread
headers) are DISTINCT conversations and must each start a NEW pending.

Reported bug: a re-sent "where is my order" (same sender + same subject, new Message-ID) was
wrongly attached to the old pending via the sender+subject fallback. That fallback is removed.

    python manage.py test apps.ingestion.tests_pending_match
"""

from django.test import TestCase, override_settings

from apps.classifier.service import ClassificationResult
from apps.ingestion import service
from apps.ingestion.tests_imap import FakeImap
from apps.ingestion.tests_smart import eml
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, SubTopic
from apps.tickets.models import PendingConversation, Ticket


class MatchPendingTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        cat = Category.objects.create(brand=self.brand, code="1", name="Shipment & Delivery Tracking")
        self.sub = SubTopic.objects.create(category=cat, code="1.1", name="Shipment Status")
        self.p = PendingConversation.objects.create(
            organization=self.org, brand=self.brand, customer_email="b@x.com", subject="where is my order",
            original_message_id="<orig@x>", last_message_id="<sys@x>",
            category="1. Shipment & Delivery Tracking", category_ref=cat, sub_topic_ref=self.sub,
            status="awaiting_evidence")

    def _match(self, **msg):
        msg.setdefault("from_email", "b@x.com")
        msg.setdefault("in_reply_to", "")
        msg.setdefault("references", [])
        msg.setdefault("subject", "")
        msg.setdefault("body_text", "")
        return service._match_pending(self.brand, msg)

    def test_in_reply_to_matches(self):
        p, reason = self._match(in_reply_to="<sys@x>", subject="anything at all")
        self.assertEqual(p, self.p)
        self.assertEqual(reason, "in_reply_to")

    def test_references_matches(self):
        p, reason = self._match(references=["<other@y>", "<orig@x>"], subject="zzz")
        self.assertEqual(p, self.p)
        self.assertEqual(reason, "references")

    # REQUIREMENT: same sender + same subject + new Message-ID (no thread headers) -> NEW.
    def test_same_sender_same_subject_new_message_id_new_pending(self):
        p, reason = self._match(subject="where is my order", message_id="<B@x>")
        self.assertIsNone(p)                      # does NOT match the old pending
        self.assertEqual(reason, "no_match")      # -> caller creates a new pending

    def test_same_sender_re_same_subject_without_headers_no_match(self):
        # Even a literal "Re:" subject with NO In-Reply-To/References is a new conversation.
        p, reason = self._match(subject="Re: where is my order")
        self.assertIsNone(p)
        self.assertEqual(reason, "no_match")

    # REQUIREMENT: same sender + a genuine Reply (In-Reply-To) -> the EXISTING pending.
    def test_same_sender_reply_matches_existing(self):
        p, reason = self._match(subject="Re: where is my order", in_reply_to="<sys@x>")
        self.assertEqual(p, self.p)
        self.assertEqual(reason, "in_reply_to")

    def test_same_sender_different_subject_no_match(self):
        p, reason = self._match(subject="my order is damage", body_text="it broke")
        self.assertIsNone(p)
        self.assertEqual(reason, "no_match")

    def test_different_sender_same_subject_no_match(self):
        p, reason = self._match(from_email="someone@else.com", subject="where is my order")
        self.assertIsNone(p)
        self.assertEqual(reason, "no_match")

    def test_no_email_fallback(self):
        # The old bug: any email from the sender matched. It must NOT anymore.
        p, reason = self._match(subject="completely unrelated question")
        self.assertIsNone(p)

    def test_ticket_reference_reason(self):
        p, reason = self._match(subject="Re: TKT-2026-000123 update", body_text="thanks")
        self.assertIsNone(p)                              # belongs to a ticket, not a pending
        self.assertEqual(reason, "ticket_reference")


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class DamageAfterTrackingTests(TestCase):
    """End-to-end: a damage complaint arriving after a tracking request starts a NEW case,
    is not swallowed by the tracking pending, and creates no duplicate ticket."""

    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        self.cat1 = Category.objects.create(brand=self.brand, code="1",
                                            name="Shipment & Delivery Tracking")
        self.sub1 = SubTopic.objects.create(category=self.cat1, code="1.1", name="Shipment Status",
                                            mandatory_inputs=["order_id"])
        self.cat3 = Category.objects.create(brand=self.brand, code="3",
                                            name="Delivery Issues (Post-Delivery)")
        self.sub3 = SubTopic.objects.create(category=self.cat3, code="3.3", name="Damaged",
                                            requires_evidence=True, mandatory_inputs=["order_id"])
        # Pre-existing OPEN tracking pending for this customer.
        self.tracking = PendingConversation.objects.create(
            organization=self.org, brand=self.brand, customer_email="buyer@example.com", subject="where is my order",
            original_message_id="<track@x>", last_message_id="<tracksys@x>",
            category="1. Shipment & Delivery Tracking", category_ref=self.cat1,
            sub_topic_ref=self.sub1, status="awaiting_evidence")

    def _classify(self, category, sub, summary, *, cat_ref, sub_ref, evid):
        def _fn(b, m):
            return ClassificationResult(
                category=category, sub_topic=sub, confidence=0.9, extracted={},
                sentiment="neutral", language="en", is_support_request=True,
                issue_summary=summary, requires_evidence=evid, requires_agent=False,
                category_ref=cat_ref, sub_topic_ref=sub_ref)
        return _fn

    def _run(self, emails, classify):
        orig = service._classify_dict
        service._classify_dict = classify
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            service._classify_dict = orig

    def test_damage_after_tracking_starts_new_case(self):
        self._run(
            [eml(subject="my product is damaged", body="it is broken",
                 message_id="<dmg@x>")],   # NEW subject, no In-Reply-To
            self._classify("3. Delivery Issues (Post-Delivery)", "3.3 Damaged", "damaged",
                           cat_ref=self.cat3, sub_ref=self.sub3, evid=True))
        # A SEPARATE pending for the damage complaint -- the tracking pending is untouched.
        self.assertEqual(PendingConversation.objects.count(), 2)
        damage = PendingConversation.objects.exclude(id=self.tracking.id).get()
        self.assertEqual(damage.subject, "my product is damaged")
        self.assertNotEqual(damage.id, self.tracking.id)
        self.assertEqual(Ticket.objects.count(), 0)            # held for evidence, no ticket

    def test_genuine_tracking_reply_still_matches(self):
        # A real reply (In-Reply-To = the tracking pending's sent message) stays in the
        # SAME tracking pending -> no new pending, no ticket (preserves the workflow).
        self._run(
            [eml(subject="Re: where is my order", body="order id 486324",
                 message_id="<r@x>", in_reply_to="<tracksys@x>", references="<track@x>")],
            self._classify("1. Shipment & Delivery Tracking", "1.1 Shipment Status", "track",
                           cat_ref=self.cat1, sub_ref=self.sub1, evid=False))
        self.assertEqual(PendingConversation.objects.count(), 1)   # no new pending
        self.assertEqual(Ticket.objects.count(), 0)                # tracking never makes a ticket

    def test_repeated_same_subject_without_headers_starts_new_pending(self):
        # Two damage emails with the SAME subject but NO thread headers (different
        # Message-IDs) are DISTINCT conversations now -> a separate pending each (the
        # sender+subject dedup fallback was removed). Neither touches the tracking pending.
        # Both emails go through ONE _run so the IMAP watermark advances per-UID.
        cls = self._classify("3. Delivery Issues (Post-Delivery)", "3.3 Damaged", "damaged",
                             cat_ref=self.cat3, sub_ref=self.sub3, evid=True)
        self._run([
            eml(subject="my product is damaged", body="broken", message_id="<d1@x>"),
            eml(subject="my product is damaged", body="still broken", message_id="<d2@x>"),
        ], cls)
        # tracking pending + TWO separate damage pendings.
        self.assertEqual(PendingConversation.objects.count(), 3)
        self.assertEqual(Ticket.objects.count(), 0)

    def test_reply_with_headers_stays_in_same_pending(self):
        # A genuine reply (In-Reply-To = the first damage email <d1@x>) folds into the SAME
        # case -> exactly ONE damage pending, never a duplicate.
        cls = self._classify("3. Delivery Issues (Post-Delivery)", "3.3 Damaged", "damaged",
                             cat_ref=self.cat3, sub_ref=self.sub3, evid=True)
        self._run([
            eml(subject="my product is damaged", body="broken", message_id="<d1@x>"),
            eml(subject="Re: my product is damaged", body="still broken",
                message_id="<d2@x>", in_reply_to="<d1@x>", references="<d1@x>"),
        ], cls)
        # tracking pending + exactly ONE damage pending (the reply matched the first).
        self.assertEqual(PendingConversation.objects.count(), 2)
        self.assertEqual(Ticket.objects.count(), 0)
