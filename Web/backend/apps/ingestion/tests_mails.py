"""
Tests for the M1–M7 outbound mail registry (EN/HI/GU language auto-pick).

    python manage.py test apps.ingestion.tests_mails
"""

from django.test import TestCase

from apps.ingestion import mails


class MailRegistryTests(TestCase):
    def test_every_mail_has_all_languages(self):
        for mail_id, variants in mails.MAILS.items():
            for lang in mails.SUPPORTED_LANGS:
                self.assertIn(lang, variants, f"{mail_id} missing {lang}")
                subject, body = variants[lang]
                self.assertTrue(subject and body, f"{mail_id}/{lang} empty")

    def test_unknown_language_falls_back_to_english(self):
        en = mails.render("M5", "en", ticket_number="TKT-1", tracking_url="http://x")
        fr = mails.render("M5", "fr", ticket_number="TKT-1", tracking_url="http://x")
        self.assertEqual(en, fr)               # unknown -> English

    def test_render_fills_placeholders_and_signature(self):
        subject, body = mails.render("M5", "en", ticket_number="TKT-2026-000123",
                                     tracking_url="https://care.deodap.in/t?id=abc")
        self.assertEqual(subject, "Support Ticket Created Successfully")
        self.assertIn("TKT-2026-000123", body)
        self.assertIn("https://care.deodap.in/t?id=abc", body)
        self.assertIn("DeoDap", body)          # signature present

    def test_missing_placeholder_is_blank_not_error(self):
        # render M6 without tracking_url -> no KeyError, blank where the var would be.
        subject, body = mails.render("M6", "en", ticket_number="TKT-9")
        self.assertIn("TKT-9", body)

    def test_hindi_and_gujarati_differ_from_english(self):
        en = mails.render("M2", "en", complaint_ref="the complaint for order DD9999")[1]
        hi = mails.render("M2", "hi", complaint_ref="ऑर्डर DD9999 की शिकायत")[1]
        gu = mails.render("M2", "gu", complaint_ref="ઓર્ડર DD9999 ની ફરિયાદ")[1]
        self.assertNotEqual(en, hi)
        self.assertNotEqual(en, gu)
        self.assertIn("DD9999", hi)            # placeholder still filled in HI


class EvidenceComplaintRefTests(TestCase):
    """The damaged-order evidence request must never read 'order your order' / 'order order'
    / a dangling 'order ' -- it reads 'the complaint for order <N>' or 'your complaint'."""

    def _photo_body(self, order_id):
        from apps.ingestion import service
        ref = service._complaint_ref(order_id, "en")
        return mails.render("M2P", "en", complaint_ref=ref)[1]

    def _assert_no_bad_phrases(self, body):
        for bad in ("order your order", "order order", "order ,", "order .",
                    "{order_ref}", "{complaint_ref}"):
            self.assertNotIn(bad, body, f"bad phrase {bad!r} in: {body!r}")

    def test_valid_order_number(self):
        body = self._photo_body("262134021")
        self.assertIn("To register the complaint for order 262134021, please reply "
                      "with a clear photo", body)
        self._assert_no_bad_phrases(body)

    def test_missing_order_number(self):
        body = self._photo_body("")
        self.assertIn("To register your complaint, please reply with a clear photo", body)
        self.assertNotIn("order", body.split("please reply")[0])   # no 'order' before the verb
        self._assert_no_bad_phrases(body)

    def test_none_order_number(self):
        body = self._photo_body(None)
        self.assertIn("To register your complaint, please reply", body)
        self._assert_no_bad_phrases(body)

    def test_empty_string_order_number(self):
        body = self._photo_body("   ")                              # whitespace-only
        self.assertIn("To register your complaint, please reply", body)
        self._assert_no_bad_phrases(body)

    def test_video_request_same_logic(self):
        from apps.ingestion import service
        with_order = mails.render("M2", "en",
                                  complaint_ref=service._complaint_ref("DD9999", "en"))[1]
        without = mails.render("M2", "en",
                               complaint_ref=service._complaint_ref(None, "en"))[1]
        self.assertIn("the complaint for order DD9999", with_order)
        self.assertIn("your complaint", without)
        for b in (with_order, without):
            self._assert_no_bad_phrases(b)

    def test_localized_clauses(self):
        from apps.ingestion import service
        self.assertEqual(service._complaint_ref("DD1", "hi"), "ऑर्डर DD1 की शिकायत")
        self.assertEqual(service._complaint_ref("", "hi"), "अपनी शिकायत")
        self.assertEqual(service._complaint_ref("DD1", "gu"), "ઓર્ડર DD1 ની ફરિયાદ")
        self.assertEqual(service._complaint_ref(None, "gu"), "તમારી ફરિયાદ")

    def test_normalize_lang(self):
        self.assertEqual(mails.normalize_lang("en-US"), "en")
        self.assertEqual(mails.normalize_lang("HI"), "hi")
        self.assertEqual(mails.normalize_lang(""), "en")
        self.assertEqual(mails.normalize_lang(None), "en")


class ConfirmationTrackingLinkTests(TestCase):
    """Every ticket confirmation email (created / updated / duplicate-found) must carry the
    SAME tracking URL when the ticket has a real Care Panel hash (the reported bug: update
    emails dropped the link)."""

    def setUp(self):
        from apps.organizations.models import Organization, Brand, Mailbox
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self, care_hash="ABChash123"):
        from apps.tickets.models import Ticket
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="buyer@example.com", subject="app issue",
            ticket_number="TKT-2026-000163",
            extracted={"care_panel_ticket_id": care_hash} if care_hash else {})

    def _last_outbound(self, ticket):
        from apps.tickets.models import Message
        return (ticket.messages.filter(direction=Message.DIRECTION_OUTBOUND)
                .order_by("created_at").last())

    def test_updated_email_includes_tracking_url(self):
        from apps.ingestion import service
        t = self._ticket(care_hash="ABChash123")
        service.send_confirmation(t, "updated")
        body = self._last_outbound(t).body_text
        self.assertIn("https://care.deodap.in/t?id=ABChash123", body)
        self.assertIn("TKT-2026-000163", body)

    def test_created_and_updated_share_same_link(self):
        from apps.ingestion import service
        t = self._ticket(care_hash="ABChash123")
        service.send_confirmation(t, "created")
        service.send_confirmation(t, "updated")
        from apps.tickets.models import Message
        bodies = list(t.messages.filter(direction=Message.DIRECTION_OUTBOUND)
                      .values_list("body_text", flat=True))
        self.assertEqual(len(bodies), 2)
        for b in bodies:
            self.assertIn("https://care.deodap.in/t?id=ABChash123", b)
