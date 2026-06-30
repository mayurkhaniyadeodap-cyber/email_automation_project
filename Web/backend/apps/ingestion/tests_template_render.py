"""
Tests: no customer email ever contains raw template placeholders like {tracking_url}
/ {edd} / {order_id} / {ticket_number}. Covers template rendering, the engine's
don't-auto-send-half-filled rule, and the send-time safety net.

    python manage.py test apps.ingestion.tests_template_render
"""

import re

from django.test import TestCase, override_settings

from apps.decision import templates
from apps.ingestion import service
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Message, Ticket

_PLACEHOLDER = re.compile(r"\{[a-zA-Z0-9_]+\}")

ROUTE_A_TEMPLATE = ("Hi! Your order {order_id} is on the way. "
                    "Track it here: {tracking_url}. Expected by {edd}.")


class TemplateRenderTests(TestCase):
    def test_route_a_template_rendering(self):
        ctx = {"order_id": "123456",
               "tracking_url": "https://tracking.example.com/123456",
               "edd": "15 Jun 2026"}
        text, unresolved = templates.render(ROUTE_A_TEMPLATE, ctx)
        self.assertEqual(unresolved, [])
        self.assertIn("123456", text)
        self.assertIn("https://tracking.example.com/123456", text)
        self.assertIn("15 Jun 2026", text)

    def test_no_unresolved_placeholders(self):
        ctx = {"order_id": "123456",
               "tracking_url": "https://tracking.example.com/123456",
               "edd": "15 Jun 2026"}
        text, _ = templates.render(ROUTE_A_TEMPLATE, ctx)
        self.assertIsNone(_PLACEHOLDER.search(text))   # nothing like {x} remains

    def test_tracking_url_present(self):
        text, _ = templates.render(ROUTE_A_TEMPLATE,
                                   {"order_id": "1", "tracking_url": "https://t/1", "edd": "x"})
        self.assertIn("https://t/1", text)
        self.assertNotIn("{tracking_url}", text)

    def test_edd_present(self):
        text, _ = templates.render(ROUTE_A_TEMPLATE,
                                   {"order_id": "1", "tracking_url": "https://t/1", "edd": "15 Jun 2026"})
        self.assertIn("15 Jun 2026", text)
        self.assertNotIn("{edd}", text)

    def test_missing_live_data_is_reported_unresolved(self):
        # No tracking_url / edd in context -> reported, NOT silently sent.
        text, unresolved = templates.render(ROUTE_A_TEMPLATE, {"order_id": "123456"})
        self.assertIn("tracking_url", unresolved)
        self.assertIn("edd", unresolved)


class SendGuardTests(TestCase):
    """The last line of defense: send_reply must never let a raw placeholder out."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.ticket = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="where is my order")

    def _outbound(self, body):
        return Message.objects.create(
            ticket=self.ticket, direction=Message.DIRECTION_OUTBOUND,
            to_email="b@x.com", subject="Re: order", body_text=body)

    def test_unresolved_placeholder_triggers_safe_fallback(self):
        msg = self._outbound("Hi! Your order 123456 is on the way. "
                             "Track it here: {tracking_url}. Expected by {edd}.")
        service.send_reply(msg)
        msg.refresh_from_db()
        # The raw placeholders are gone; a safe fallback was substituted.
        self.assertIsNone(_PLACEHOLDER.search(msg.body_text))
        self.assertNotIn("{tracking_url}", msg.body_text)
        self.assertNotIn("{edd}", msg.body_text)
        self.assertIn("DeoDap", msg.body_text)
        self.assertTrue(self.ticket.audit_log.filter(event="template_render_error").exists())

    def test_clean_body_is_left_untouched(self):
        clean = "Hi! Your order 123456 is on the way. Track: https://t/123456. Expected 15 Jun 2026."
        msg = self._outbound(clean)
        service.send_reply(msg)
        msg.refresh_from_db()
        self.assertEqual(msg.body_text, clean)   # unchanged
        self.assertFalse(self.ticket.audit_log.filter(event="template_render_error").exists())

    def test_all_listed_placeholders_are_caught(self):
        for ph in ["{tracking_url}", "{edd}", "{order_id}", "{ticket_number}", "{tracking_link}"]:
            msg = self._outbound(f"Your link: {ph}")
            service.send_reply(msg)
            msg.refresh_from_db()
            self.assertIsNone(_PLACEHOLDER.search(msg.body_text), ph)


@override_settings(PUBLIC_BASE_URL="https://support.deodap.in")
class OrderStatusFallbackTests(TestCase):
    """'Where is my order' auto-answers with a WORKING tracking link (Django portal /t
    when live data is absent) and NO edd placeholder."""

    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        from apps.taxonomy.models import Category, Rule, SubTopic
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        self.cat = Category.objects.create(brand=self.brand, code="1",
                                           name="Shipment & Delivery Tracking")
        self.sub = SubTopic.objects.create(category=self.cat, code="1.1",
                                           name="Shipment Status", mandatory_inputs=["order_id"])
        Rule.objects.create(sub_topic=self.sub, position=1, condition="Always",
                            action=Rule.ACTION_INFO_ONLY,
                            then_response="Hi! We've received your request for order "
                            "{order_id}. You can track the status here: {tracking_url}.")
        from apps.taxonomy.models import Template
        Template.objects.create(sub_topic=self.sub, name="default",
                                body="Hi! We've received your request for order {order_id}. "
                                "You can track the status here: {tracking_url}.")

    def _ticket(self):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="where is my order",
            category_ref=self.cat, sub_topic_ref=self.sub, ai_confidence=0.9,
            classification_status=Ticket.CLS_CLASSIFIED, extracted={"order_id": "123456"})

    def test_order_status_auto_reply_no_placeholders_no_internal_link(self):
        # With no live tracking link and no real Care Panel hash, the template's
        # {tracking_url} can't be filled -> the NO_TICKET (cat 1) policy regenerates the
        # answer via the AI responder. It must contain NO care.deodap.in/<internal> link.
        from apps.classifier import service as classifier

        class FakeProvider:
            def generate_text(self, system, user):
                return "Hi! Your order 123456 is being processed. Track it in the DeoDap app."

        t = self._ticket()
        orig = classifier.build_provider
        classifier.build_provider = lambda s: FakeProvider()
        try:
            service._auto_decide(t)
        finally:
            classifier.build_provider = orig
        t.refresh_from_db()
        out = t.messages.filter(direction=Message.DIRECTION_OUTBOUND).last()
        self.assertIsNotNone(out)
        self.assertIn("123456", out.body_text)
        self.assertIsNone(_PLACEHOLDER.search(out.body_text))   # no raw {x} placeholder
        self.assertNotIn("{tracking_url}", out.body_text)
        # No real Care Panel hash -> NEVER a care.deodap.in/<internal-hash> 404 link.
        self.assertNotIn("care.deodap.in/t?id=", out.body_text)
        # the internal hash is still stored so our own /t page can resolve it
        self.assertTrue(t.extracted.get("tracking_hash"))

    def test_live_tracking_url_is_preferred_over_internal(self):
        t = self._ticket()
        t.extracted = {**t.extracted, "tracking_url": "https://ship.deodap.com/t/AWB9"}
        t.save(update_fields=["extracted"])
        service._auto_decide(t)
        out = t.messages.filter(direction=Message.DIRECTION_OUTBOUND).last()
        self.assertIn("https://ship.deodap.com/t/AWB9", out.body_text)
