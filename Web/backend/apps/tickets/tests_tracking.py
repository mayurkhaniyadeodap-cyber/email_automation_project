"""
Tests for the public ticket tracking page (GET /t?id=<hash>) and the guarantee that
every ticket gets a resolvable tracking link (root cause: links 404'd because they
pointed at the external care.deodap.in instead of our own /t route).

    python manage.py test apps.tickets.tests_tracking
"""

from django.test import Client, TestCase, override_settings  # type: ignore
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.ingestion import service
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Ticket


class TrackingPageTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self, **extra):
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="b@x.com", subject="damaged product",
            issue_summary="damaged item", **extra)

    # --- create ticket -> internal hash -> our /t portal resolves it (HTTP 200) -------
    def test_create_ticket_internal_hash_resolves_on_portal(self):
        t = self._ticket()
        service._ensure_tracking(t)                  # mints internal hash (no care link)
        t.refresh_from_db()
        # ticket_number + internal hash recorded; no care.deodap.in/<internal> link emitted
        self.assertTrue(t.ticket_number)
        self.assertTrue(t.extracted.get("tracking_hash"))
        self.assertEqual(t.tracking_url, "")         # no real Care Panel hash -> no link

        # our own /t portal still resolves the internal hash (200, no 404)
        resp = self.client.get(f"/t?id={t.extracted['tracking_hash']}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(t.ticket_number, resp.content.decode())

    def test_unknown_hash_returns_404(self):
        resp = self.client.get("/t?id=doesnotexist")
        self.assertEqual(resp.status_code, 404)

    def test_missing_id_returns_404(self):
        self.assertEqual(self.client.get("/t").status_code, 404)

    def test_resolves_by_care_panel_ticket_id(self):
        t = self._ticket(extracted={"care_panel_ticket_id": "RealHash99"})
        resp = self.client.get("/t?id=RealHash99")
        self.assertEqual(resp.status_code, 200)
        t.refresh_from_db()
        self.assertEqual(t.extracted.get("tracking_hash"), "RealHash99")  # healed (#7)

    # --- a REAL Care Panel hash -> care.deodap.in link; otherwise NO link ------------
    def test_real_care_panel_hash_uses_care_base(self):
        t = self._ticket(extracted={"care_panel_ticket_id": "RealHash99"})
        url = service._ensure_tracking(t)
        self.assertEqual(url, "https://care.deodap.in/t?id=RealHash99")

    @override_settings(PUBLIC_BASE_URL="http://192.168.1.2:8000")
    def test_internal_only_ticket_gets_no_link(self):
        # No real Care Panel hash + a stray LAN PUBLIC_BASE_URL -> still NO link, and
        # certainly never a localhost/LAN or internal-hash care.deodap.in link.
        t = self._ticket()
        url = service._ensure_tracking(t)
        self.assertEqual(url, "")
        for bad in ("192.168", "127.0.0.1", "localhost", "care.deodap.in"):
            self.assertNotIn(bad, url)

    @override_settings(PUBLIC_BASE_URL="")
    def test_internal_only_ticket_no_link_when_base_unset(self):
        t = self._ticket()
        self.assertEqual(service._ensure_tracking(t), "")

    def test_care_panel_link_is_kept_but_hash_recorded(self):
        t = self._ticket(tracking_url="https://care.deodap.in/t?id=CP123",
                         ticket_number="2606090601",
                         extracted={"care_panel_ticket_id": "CP123"})
        url = service._ensure_tracking(t)
        t.refresh_from_db()
        self.assertEqual(url, "https://care.deodap.in/t?id=CP123")   # real CP link kept
        self.assertEqual(t.extracted.get("tracking_hash"), "CP123")  # hash recorded
        # and the real Care Panel hash resolves on our page too
        self.assertEqual(self.client.get("/t?id=CP123").status_code, 200)


class ConversationSectionTests(TestCase):
    """The NEW Conversation section: the complete email thread built from the ticket's messages
    (sender name/type, email, datetime, body, per-message attachments). Tested directly (no HTTP
    client) so it is independent of the Django test-client / Python version."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        self.ticket = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="buyer@example.com", subject="where is my order",
            extracted={"tracking_hash": "hash123", "name": "Rahul",
                       "customer_name": "Rahul", "customer_name_source": "shopify_verified"})

    def _seed(self):
        from apps.tickets.models import Message, Attachment
        Message.objects.create(ticket=self.ticket, direction=Message.DIRECTION_INBOUND,
                               from_email="buyer@example.com", subject="where is my order",
                               body_text="Where is my order?")               # 1 initial customer
        Message.objects.create(ticket=self.ticket, direction=Message.DIRECTION_OUTBOUND,
                               from_email="care@deodap.com", subject="Re: where is my order",
                               body_text="Here is your status.")             # 2 support reply
        m3 = Message.objects.create(ticket=self.ticket, direction=Message.DIRECTION_INBOUND,
                                    from_email="buyer@example.com", body_text="Photo attached.")
        a = Attachment(ticket=self.ticket, message=m3, filename="photo.png",
                       content_type="image/png", size=3)
        a.file.save("photo.png", ContentFile(b"IMG"), save=True)              # 3 customer + image
        Message.objects.create(ticket=self.ticket, direction=Message.DIRECTION_OUTBOUND,
                               from_email="care@deodap.com", body_text="draft", is_draft=True)  # excluded

    def test_build_conversation_full_thread_with_attachment(self):
        from apps.tickets.tracking import _build_conversation
        self._seed()
        convo = _build_conversation(self.ticket, "hash123")
        self.assertEqual(len(convo), 3)                                        # draft excluded
        self.assertEqual([c["sender_type"] for c in convo],
                         ["Customer", "DeoDap Support", "Customer"])           # chronological
        self.assertEqual(convo[0]["sender_name"], "Rahul")                     # verified name
        self.assertEqual(convo[1]["sender_name"], "DeoDap Support")
        self.assertEqual(convo[0]["email"], "buyer@example.com")
        self.assertEqual(convo[0]["subject"], "where is my order")             # subject shown
        self.assertEqual(convo[2]["attachments"][0]["kind"], "image")
        self.assertIn("id=hash123", convo[2]["attachments"][0]["url"])

    def test_customer_info_uses_shopify_verified_identity(self):
        # After Shopify verification: name/email come from the verified order owner, NEVER the
        # email username. (Reported bug: verified ticket still showed the sender username.)
        from apps.tickets.tracking import _customer, _build_conversation
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="dabhichintan2134@gmail.com", subject="Damage order",
            extracted={"tracking_hash": "hv", "customer_name": "Aayat .",
                       "customer_name_source": "shopify_verified",
                       "customer_email": "aayat@shopcust.com", "phone": "9140505423",
                       "order_id": "262277643"})
        from apps.tickets.models import Message
        Message.objects.create(ticket=t, direction=Message.DIRECTION_INBOUND,
                               from_email="dabhichintan2134@gmail.com", body_text="hi")
        c = _customer(t)
        self.assertEqual(c["name"], "Aayat .")                 # Shopify name, NOT 'dabhichintan2134'
        self.assertNotEqual(c["name"], "dabhichintan2134")
        self.assertEqual(c["email"], "aayat@shopcust.com")     # verified email
        self.assertEqual(c["phone"], "9140505423")
        self.assertEqual(c["order_id"], "262277643")
        self.assertEqual(_build_conversation(t, "hv")[0]["sender_name"], "Aayat .")

    def test_unverified_customer_name_is_username_never_display_name(self):
        # No verified order -> fall back to the email USERNAME, never the Gmail display name.
        from apps.tickets.tracking import _customer, _build_conversation
        from apps.tickets.serializers import TicketDetailSerializer
        from apps.tickets.models import Message
        t = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="someone@gmail.com", subject="hi",
            extracted={"tracking_hash": "h9", "name": "Gmail Display Name",
                       "sender_name": "Gmail Display Name"})   # NOT shopify_verified
        Message.objects.create(ticket=t, direction=Message.DIRECTION_INBOUND,
                               from_email="someone@gmail.com", subject="hi", body_text="hello")
        self.assertEqual(_customer(t)["name"], "someone")      # username fallback
        self.assertEqual(_build_conversation(t, "h9")[0]["sender_name"], "someone")
        self.assertNotEqual(_build_conversation(t, "h9")[0]["sender_name"], "Gmail Display Name")
        self.assertEqual(TicketDetailSerializer(t).data["conversation"][0]["sender_name"],
                         "someone")

    def test_conversation_strips_gmail_quoted_history(self):
        from apps.tickets.tracking import _build_conversation
        from apps.tickets.models import Message
        Message.objects.create(
            ticket=self.ticket, direction=Message.DIRECTION_INBOUND,
            from_email="buyer@example.com", body_text=(
                "Attached the photo.\n\n"
                "On Thu, Jul 2, 2026 at 10:00 AM DeoDap Support <care@deodap.com> wrote:\n"
                "> Please upload a clear photo.\n> Regards, DeoDap"))
        convo = _build_conversation(self.ticket, "hash123")
        body = convo[0]["body"]
        self.assertIn("Attached the photo.", body)
        self.assertNotIn("wrote:", body)
        self.assertNotIn("Please upload a clear photo", body)

    def test_activity_timeline_removed_from_portal(self):
        from django.template.loader import render_to_string
        from apps.tickets import tracking as T
        self._seed()
        ctx = {"ticket": self.ticket, "hash_id": "hash123", "number": "N1",
               "status_label": "In process", "status_code": self.ticket.status,
               "status_badge": "primary", "category": "General", "issue": "x",
               "customer": T._customer(self.ticket),
               "conversation": T._build_conversation(self.ticket, "hash123"),
               "media": T._build_media(self.ticket, "hash123"),
               "progress": T._build_progress(self.ticket), "sent": False}
        html = render_to_string("tracking/ticket.html", ctx)
        self.assertNotIn("Activity Timeline", html)            # timeline removed
        self.assertIn(">Conversation<", html.replace(" ", ""))  # conversation kept
        self.assertIn("Media Files", html)                     # media kept

    def test_ticket_detail_api_adds_conversation_and_keeps_existing_fields(self):
        from apps.tickets.serializers import TicketDetailSerializer
        self._seed()
        data = TicketDetailSerializer(self.ticket).data
        # NEW field present + correct
        self.assertIn("conversation", data)
        self.assertEqual(len(data["conversation"]), 3)
        self.assertEqual(data["conversation"][0]["sender_type"], "Customer")
        self.assertEqual(data["conversation"][1]["sender_type"], "DeoDap Support")
        self.assertEqual(data["conversation"][2]["attachments"][0]["filename"], "photo.png")
        # BACKWARD COMPATIBILITY: existing fields untouched
        for f in ("id", "ticket_id", "messages", "audit_log", "status", "customer_email"):
            self.assertIn(f, data)

    def test_conversation_empty_when_no_messages(self):
        from apps.tickets.tracking import _build_conversation
        self.assertEqual(_build_conversation(self.ticket, "hash123"), [])


class TrackingPortalTests(TestCase):
    """The redesigned portal: header, customer info, timeline, media, progress, reply."""

    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        self.client = Client()
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k")
        self.ticket = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="buyer@example.com", subject="my product is damage",
            issue_summary="Customer received a damaged product and wants to return it",
            status=Ticket.STATUS_AWAITING_AGENT, ticket_number="TKT-2026-000087",
            category="Return, Refund & Replacement",
            extracted={"tracking_hash": "abc1234567", "phone": "9353711685",
                       "order_id": "455556", "name": "Uttam"})

    def _get(self):
        return self.client.get("/t?id=abc1234567")

    def test_header_and_customer_info_from_real_data(self):
        from apps.tickets.models import Message, AuditLogEntry, Attachment
        AuditLogEntry.objects.create(ticket=self.ticket, actor="system", event="ticket_created", detail={})
        Message.objects.create(ticket=self.ticket, direction=Message.DIRECTION_INBOUND,
                               from_email="buyer@example.com", body_text="Customer received a damaged product")
        html = self._get().content.decode()
        self.assertEqual(self._get().status_code, 200)
        # header
        self.assertIn("TKT-2026-000087", html)
        self.assertIn("Awaiting Agent", html)
        self.assertIn("Return, Refund &amp; Replacement", html)  # category (escaped)
        # customer info
        self.assertIn("Uttam", html)
        self.assertIn("buyer@example.com", html)
        self.assertIn("9353711685", html)
        self.assertIn("455556", html)
        # timeline (real message + event)
        self.assertIn("Customer received a damaged product", html)
        self.assertIn("Support ticket created", html)
        # progress stages
        self.assertIn("Awaiting Agent", html)
        self.assertIn("Resolved", html)

    def test_media_image_and_video_render(self):
        from apps.tickets.models import Attachment
        Attachment.objects.create(ticket=self.ticket, filename="photo.png", content_type="image/png")
        Attachment.objects.create(ticket=self.ticket, filename="clip.mp4", content_type="video/mp4")
        html = self._get().content.decode()
        self.assertIn("/t/file?id=abc1234567&amp;a=", html)   # scoped media url
        self.assertIn("<video", html)                          # inline video player
        self.assertIn("<img", html)                            # image thumbnail

    def test_media_file_serving_is_scoped(self):
        from django.core.files.base import ContentFile  # type: ignore
        from apps.tickets.models import Attachment
        att = Attachment(ticket=self.ticket, filename="p.png", content_type="image/png")
        att.file.save("p.png", ContentFile(b"\x89PNGdata"), save=True)
        # correct hash + attachment -> served
        r = self.client.get(f"/t/file?id=abc1234567&a={att.id}")
        self.assertEqual(r.status_code, 200)
        # wrong hash -> 404 (cannot read another ticket's files)
        self.assertEqual(self.client.get(f"/t/file?id=WRONGHASH&a={att.id}").status_code, 404)

    def test_customer_reply_adds_message_and_attachment(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from apps.tickets.models import Message
        before = self.ticket.messages.count()
        r = self.client.post("/t?id=abc1234567", {
            "id": "abc1234567", "comment": "Here is the photo as requested",
            "files": SimpleUploadedFile("evidence.jpg", b"\xff\xd8jpeg", content_type="image/jpeg"),
        })
        self.assertEqual(r.status_code, 302)                   # PRG redirect
        self.assertIn("sent=1", r["Location"])
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.messages.filter(direction=Message.DIRECTION_INBOUND).count(), before + 1)
        self.assertTrue(self.ticket.attachments.filter(filename="evidence.jpg").exists())
        self.assertTrue(self.ticket.audit_log.filter(event="portal_reply").exists())

    def test_empty_reply_is_ignored(self):
        before = self.ticket.messages.count()
        r = self.client.post("/t?id=abc1234567", {"id": "abc1234567", "comment": ""})
        self.assertEqual(r.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.messages.count(), before)


class BuildTrackingUrlTests(TestCase):
    """The single tracking-URL builder: Django-portal links only -- never localhost,
    internal IPs, or the external Care Panel domain."""

    def test_is_local_base_detects_local_and_internal(self):
        for bad in ["", "http://127.0.0.1:8000", "http://localhost:8000",
                    "http://192.168.1.2:8000", "http://10.0.0.5", "http://0.0.0.0:8000",
                    "http://[::1]:8000", "http://box.local"]:
            self.assertTrue(service._is_local_base(bad), bad)
        for good in ["https://care.deodap.in", "https://support.deodap.in",
                     "http://testserver", "https://support.example.com"]:
            self.assertFalse(service._is_local_base(good), good)

    def test_builds_care_panel_url_with_hash(self):
        url = service.build_tracking_url(hash_id="b6129e6ff6", ticket_id="TKT-2026-000090")
        self.assertEqual(url, "https://care.deodap.in/t?id=b6129e6ff6")

    @override_settings(PUBLIC_BASE_URL="")
    def test_url_is_care_panel_even_when_public_base_unset(self):
        # Host is hard-coded -> a missing PUBLIC_BASE_URL can never yield a localhost link.
        self.assertEqual(service.build_tracking_url(hash_id="abc", ticket_id="TKT-1"),
                         "https://care.deodap.in/t?id=abc")

    @override_settings(PUBLIC_BASE_URL="http://192.168.1.2:8000")
    def test_url_ignores_lan_public_base(self):
        # Even a LAN PUBLIC_BASE_URL is ignored -> link is always care.deodap.in.
        url = service.build_tracking_url(hash_id="abc", ticket_id="TKT-1")
        self.assertEqual(url, "https://care.deodap.in/t?id=abc")
        self.assertNotIn("192.168", url)

    def test_logs_tracking_url_generated(self):
        with self.assertLogs("apps.ingestion.service", level="INFO") as cm:
            service.build_tracking_url(hash_id="zz9", ticket_id="TKT-2026-000090")
        line = "\n".join(cm.output)
        self.assertIn("TRACKING_URL_GENERATED", line)
        self.assertIn("TKT-2026-000090", line)
        self.assertIn("https://care.deodap.in/t?id=zz9", line)
