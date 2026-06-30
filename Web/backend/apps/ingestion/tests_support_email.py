"""
Dynamic Support Email config: the Care Panel fetches ONE Gmail inbox; aliases are used ONLY for
sending. Any inbound email sent BY a configured SupportEmail (primary OR alias) is NEVER imported
(prevents the self-reply loop). Fully dynamic -- no hardcoded addresses.

    python manage.py test apps.ingestion.tests_support_email
"""
from django.test import TestCase

from apps.brand_settings.models import SupportEmail
from apps.ingestion import service
from apps.organizations.models import Brand, Mailbox, Organization
from apps.tickets.models import Message, Ticket


class SupportEmailFetchFilterTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand,
                                              email_address="chintandeodap2134@gmail.com")
        # Primary inbox + one sending alias -- both ACTIVE.
        SupportEmail.objects.create(brand=self.brand, email="chintandeodap2134@gmail.com",
                                    is_primary=True, is_active=True)
        SupportEmail.objects.create(brand=self.brand, email="deodap.4300@gmail.com",
                                    is_primary=False, is_active=True)
        # Classification must NOT run for a skipped email.
        self._oc = service._classify_dict
        service._classify_dict = lambda b, m: (_ for _ in ()).throw(
            AssertionError("classification ran for an own-sent email"))

    def tearDown(self):
        service._classify_dict = self._oc

    def _run(self, **msg):
        msg.setdefault("subject", "hello")
        msg.setdefault("body_text", "where is my order")
        return service.handle_incoming_email(self.mailbox, msg)

    # --- required tests ----------------------------------------------------------------------
    def test_primary_email_ignored(self):
        t, m, created = self._run(from_email="chintandeodap2134@gmail.com",
                                  message_id="<p1@x>", gmail_message_id="<p1@x>")
        self.assertIsNone(t)
        self.assertEqual(Ticket.objects.count(), 0)

    def test_alias_ignored(self):
        t, m, created = self._run(from_email="deodap.4300@gmail.com",
                                  message_id="<a1@x>", gmail_message_id="<a1@x>")
        self.assertIsNone(t)
        self.assertEqual(Ticket.objects.count(), 0)

    def test_alias_in_return_path_or_sender_header_ignored(self):
        # From is the customer, but Return-Path / Sender is our alias (forwarded loop) -> skip.
        t, m, created = self._run(from_email="customer@x.com", message_id="<a2@x>",
                                  gmail_message_id="<a2@x>",
                                  headers={"Return-Path": "<deodap.4300@gmail.com>"})
        self.assertIsNone(t)
        self.assertEqual(Ticket.objects.count(), 0)

    def test_inactive_alias_is_not_skipped(self):
        SupportEmail.objects.filter(email="deodap.4300@gmail.com").update(is_active=False)
        service._classify_dict = self._oc  # allow normal processing
        t, m, created = self._run(from_email="deodap.4300@gmail.com", message_id="<a3@x>",
                                  gmail_message_id="<a3@x>")
        # No longer an active support email -> imported as a normal ticket.
        self.assertIsNotNone(t)

    def test_unknown_sender_imported(self):
        service._classify_dict = self._oc  # allow normal processing for a real customer
        t, m, created = self._run(from_email="realcustomer@gmail.com", message_id="<c1@x>",
                                  gmail_message_id="<c1@x>")
        self.assertIsNotNone(t)
        self.assertTrue(created)

    def test_duplicate_message_id_ignored(self):
        service._classify_dict = self._oc
        first = self._run(from_email="cust@x.com", message_id="<d1@x>", gmail_message_id="<d1@x>")
        self.assertIsNotNone(first[0])
        n = Ticket.objects.count()
        # Same Gmail Message-ID a second time -> no new ticket / message.
        second = self._run(from_email="cust@x.com", message_id="<d1@x>", gmail_message_id="<d1@x>")
        self.assertEqual(Ticket.objects.count(), n)
        self.assertEqual(Message.objects.filter(gmail_message_id="<d1@x>").count(), 1)

    def test_match_is_case_insensitive(self):
        t, m, created = self._run(from_email="DeoDap.4300@GMAIL.com", message_id="<u1@x>",
                                  gmail_message_id="<u1@x>")
        self.assertIsNone(t)
        self.assertEqual(Ticket.objects.count(), 0)


class ReplyFromAddressTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="primary@x.com")

    def test_reply_from_uses_primary_support_email(self):
        from django.test import override_settings
        SupportEmail.objects.create(brand=self.brand, email="primary@x.com", is_primary=True)
        with override_settings(REPLY_FROM=""):
            self.assertEqual(service.reply_from_address(self.mailbox), "primary@x.com")

    def test_reply_from_prefers_explicit_setting(self):
        from django.test import override_settings
        with override_settings(REPLY_FROM="Support@DeoDap.com"):
            self.assertEqual(service.reply_from_address(self.mailbox), "support@deodap.com")


class ChosenSenderTests(TestCase):
    """STEP 4.1 -- the agent may choose the From; only a valid SupportEmail alias is honored."""

    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="primary@x.com")
        SupportEmail.objects.create(brand=self.brand, email="primary@x.com", is_primary=True)
        SupportEmail.objects.create(brand=self.brand, email="alias@x.com")

    def test_valid_alias_is_honored(self):
        from django.test import override_settings
        with override_settings(REPLY_FROM=""):
            self.assertEqual(
                service.resolve_sender_email(self.mailbox, "ALIAS@x.com",
                                             default="primary@x.com"), "alias@x.com")

    def test_reply_to_is_primary_inbox_not_alias(self):
        # When a reply is sent FROM an alias, Reply-To must be the PRIMARY fetched inbox so the
        # customer's reply comes back to the polled mailbox (not the alias's separate inbox).
        self.assertEqual(service.primary_inbox_address(self.mailbox), "primary@x.com")
        captured = {}
        import apps.ingestion.smtp_client as smtp
        # Patch the low-level SMTP send to capture from_addr + reply_to.
        self._real = smtp.send_email
        def fake(**kw):
            captured.update(from_addr=kw.get("from_addr"), reply_to=kw.get("reply_to"))
            return "<sent>"
        smtp.send_email = fake
        from django.test import override_settings
        self.addCleanup(lambda: setattr(smtp, "send_email", self._real))
        with override_settings(EMAIL_PROVIDER="imap", SMTP_HOST="h", IMAP_USER="primary@x.com",
                               IMAP_PASSWORD="p"):
            service._send_customer_email("cust@x.com", "Re: hi", "body",
                                         from_email="alias@x.com",
                                         reply_to=service.primary_inbox_address(self.mailbox))
        self.assertEqual(captured["from_addr"], "alias@x.com")     # From = the chosen alias
        self.assertEqual(captured["reply_to"], "primary@x.com")    # Reply-To = the fetched inbox

    def test_unknown_address_falls_back_to_default(self):
        # An arbitrary From is NOT honored (no spoofing) -> default (received mailbox) is used.
        self.assertEqual(
            service.resolve_sender_email(self.mailbox, "stranger@evil.com",
                                         default="primary@x.com"), "primary@x.com")

    def test_inactive_alias_not_honored(self):
        SupportEmail.objects.filter(email="alias@x.com").update(is_active=False)
        self.assertEqual(
            service.resolve_sender_email(self.mailbox, "alias@x.com",
                                         default="primary@x.com"), "primary@x.com")

    def test_ticket_reply_sends_from_chosen_alias(self):
        from django.contrib.auth import get_user_model
        from django.test import override_settings
        from rest_framework.test import APIClient
        from apps.analytics.models import ManualReplyLog
        captured = {}
        self._os = service.send_reply
        service.send_reply = lambda m, client=None: captured.update(from_email=m.from_email) or "<s>"
        self.addCleanup(lambda: setattr(service, "send_reply", self._os))
        t = Ticket.objects.create(organization=self.org, brand=self.brand, mailbox=self.mailbox,
                                  customer_email="c@x.com", subject="hi")
        agent = get_user_model().objects.create_superuser("a", "a@x.com", "x")
        client = APIClient(); client.force_authenticate(agent)
        with override_settings(REPLY_FROM=""):
            r = client.post(f"/api/tickets/{t.id}/reply/",
                            {"body_text": "hello", "from_email": "alias@x.com"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(captured.get("from_email"), "alias@x.com")   # Message.from_email = alias
        log = ManualReplyLog.objects.filter(ticket=t).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.sender_email, "alias@x.com")             # recorded sender


class EmployeeAttributionTests(TestCase):
    """Replies from ANY alias still count for the SAME employee (attribution is by the agent,
    not the From address)."""

    def test_alias_replies_group_under_same_employee(self):
        from apps.analytics import dashboard, logging as alog
        org = Organization.objects.create(name="DeoDap")
        brand = Brand.objects.create(organization=org, name="DeoDap.in")
        from django.contrib.auth import get_user_model
        agent = get_user_model().objects.create_user("chintan", "chintan@deodap.com", "x")
        # Same agent, two DIFFERENT alias From addresses.
        alog.log_manual_reply(brand=brand, employee=agent, customer_email="a@x.com",
                              subject="re", message_id="<1>", body="hi",
                              sender_email="chintandeodap2134@gmail.com")
        alog.log_manual_reply(brand=brand, employee=agent, customer_email="b@x.com",
                              subject="re", message_id="<2>", body="hi",
                              sender_email="deodap.4300@gmail.com")
        perf = dashboard.employee_performance([brand.id])
        rows = [r for r in perf if r["employee_email"] == "chintan@deodap.com"]
        self.assertEqual(len(rows), 1)                 # ONE employee row, not two
        self.assertEqual(rows[0]["manual_replies"], 2)  # both alias replies counted

    def test_replies_credited_to_alias_owner(self):
        # Aliases owned by a named employee -> Employee Performance credits the OWNER, not the
        # shared 'admin' login.
        from apps.analytics import dashboard, logging as alog
        org = Organization.objects.create(name="DeoDap")
        brand = Brand.objects.create(organization=org, name="DeoDap.in")
        from django.contrib.auth import get_user_model
        admin = get_user_model().objects.create_user("admin", "admin@deodap.com", "x")
        SupportEmail.objects.create(brand=brand, email="deodap.4300@gmail.com",
                                    owner_name="Chintan Dabhi")
        SupportEmail.objects.create(brand=brand, email="deodap.5000@gmail.com",
                                    owner_name="Chintan Dabhi")     # second alias, same owner
        for s in ("deodap.4300@gmail.com", "deodap.5000@gmail.com"):
            alog.log_manual_reply(brand=brand, employee=admin, customer_email="c@x.com",
                                  subject="re", message_id="<m>", body="hi", sender_email=s)
        perf = dashboard.employee_performance([brand.id])
        chintan = [r for r in perf if r["employee_name"] == "Chintan Dabhi"]
        self.assertEqual(len(chintan), 1)               # both aliases -> ONE owner row
        self.assertEqual(chintan[0]["manual_replies"], 2)
        # the report rows also show the owner as the employee
        rows = dashboard.manual_reply_rows([brand.id])
        self.assertTrue(all(r["employee_name"] == "Chintan Dabhi" for r in rows))
