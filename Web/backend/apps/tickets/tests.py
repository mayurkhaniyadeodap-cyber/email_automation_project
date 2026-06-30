"""
Tests for the ticket queue + Ignored-tab workflow (doc sections 3 & 11):
list filtering, manual ignore / un-ignore, reply-in-thread, and the retro
apply_ignore_gate sweep.

    python manage.py test apps.tickets
"""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.brand_settings.models import BlockListEntry
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, SubTopic
from apps.tickets.models import Message, Ticket

User = get_user_model()


class BaseFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(
            brand=self.brand, email_address="care@deodap.com"
        )
        self.user = User.objects.create_user("agent", password="pw")
        self.org.members.add(self.user)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def make_ticket(self, *, subject="Order help", from_email="buyer@example.com",
                    ignored=False, status=Ticket.STATUS_NEW, headers=None, sub=None):
        ticket = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            thread_id=f"t-{subject}", customer_email=from_email, subject=subject,
            is_ignored=ignored, status=Ticket.STATUS_IGNORED if ignored else status,
            sub_topic_ref=sub,
        )
        Message.objects.create(
            ticket=ticket, direction=Message.DIRECTION_INBOUND,
            from_email=from_email, to_email=self.mailbox.email_address,
            subject=subject, body_text="help me", headers=headers or {},
        )
        return ticket


class QueueFilterTests(BaseFixture):
    def setUp(self):
        super().setUp()
        self.open = self.make_ticket(subject="open one")
        self.ignored = self.make_ticket(subject="junk", ignored=True)

    def test_list_hides_ignored_by_default(self):
        resp = self.api.get("/api/tickets/")
        ids = [t["id"] for t in resp.data["results"]]
        self.assertIn(self.open.id, ids)
        self.assertNotIn(self.ignored.id, ids)

    def test_ignored_tab_shows_only_ignored(self):
        resp = self.api.get("/api/tickets/?ignored=true")
        ids = [t["id"] for t in resp.data["results"]]
        self.assertEqual(ids, [self.ignored.id])

    def test_ignored_all_shows_both(self):
        resp = self.api.get("/api/tickets/?ignored=all")
        self.assertEqual(resp.data["count"], 2)

    def test_retrieve_ignored_ticket_by_id_still_works(self):
        resp = self.api.get(f"/api/tickets/{self.ignored.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["is_ignored"])


class IgnoreActionTests(BaseFixture):
    def test_manual_ignore_moves_to_tab_and_audits(self):
        ticket = self.make_ticket()
        resp = self.api.post(
            f"/api/tickets/{ticket.id}/ignore/", {"reason": "spammy"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertTrue(ticket.is_ignored)
        self.assertEqual(ticket.status, Ticket.STATUS_IGNORED)
        self.assertEqual(ticket.ignored_reason, "spammy")
        entry = ticket.audit_log.get(event="ignored")
        self.assertTrue(entry.detail["manual"])
        self.assertEqual(entry.actor, "agent")

    def test_unignore_restores_to_new(self):
        ticket = self.make_ticket(ignored=True)
        resp = self.api.post(f"/api/tickets/{ticket.id}/unignore/")
        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertFalse(ticket.is_ignored)
        self.assertEqual(ticket.status, Ticket.STATUS_NEW)
        self.assertEqual(ticket.ignored_reason, "")
        self.assertTrue(ticket.audit_log.filter(event="unignored").exists())

    def test_unignore_restores_classified_when_already_tagged(self):
        cat = Category.objects.create(brand=self.brand, code="3", name="Delivery")
        sub = SubTopic.objects.create(category=cat, code="3.3", name="Damaged")
        ticket = self.make_ticket(ignored=True, sub=sub)
        self.api.post(f"/api/tickets/{ticket.id}/unignore/")
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_CLASSIFIED)

    def test_unignore_on_open_ticket_400(self):
        ticket = self.make_ticket()
        resp = self.api.post(f"/api/tickets/{ticket.id}/unignore/")
        self.assertEqual(resp.status_code, 400)


class ReplyActionTests(BaseFixture):
    def test_reply_records_outbound_message(self):
        ticket = self.make_ticket()
        resp = self.api.post(
            f"/api/tickets/{ticket.id}/reply/",
            {"body_text": "On its way!"}, format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(
            ticket.messages.filter(direction=Message.DIRECTION_OUTBOUND).count(), 1
        )
        self.assertTrue(ticket.audit_log.filter(event="reply_sent").exists())

    def test_draft_reply_not_marked_sent(self):
        ticket = self.make_ticket()
        resp = self.api.post(
            f"/api/tickets/{ticket.id}/reply/",
            {"body_text": "draft text", "is_draft": True}, format="json",
        )
        self.assertEqual(resp.status_code, 201)
        msg = ticket.messages.get(direction=Message.DIRECTION_OUTBOUND)
        self.assertTrue(msg.is_draft)
        self.assertIsNone(msg.sent_at)
        self.assertTrue(ticket.audit_log.filter(event="draft_created").exists())

    def test_reply_requires_body(self):
        ticket = self.make_ticket()
        resp = self.api.post(f"/api/tickets/{ticket.id}/reply/", {}, format="json")
        self.assertEqual(resp.status_code, 400)


class ScopingTests(BaseFixture):
    def test_user_cannot_see_other_orgs_tickets(self):
        other_org = Organization.objects.create(name="Other")
        other_brand = Brand.objects.create(organization=other_org, name="OtherBrand")
        Ticket.objects.create(
            organization=other_org, brand=other_brand,
            customer_email="x@y.com", subject="not mine",
        )
        resp = self.api.get("/api/tickets/?ignored=all")
        subjects = [t["subject"] for t in resp.data["results"]]
        self.assertNotIn("not mine", subjects)


class ApplyIgnoreGateCommandTests(BaseFixture):
    def test_retro_ignore_commits_matches(self):
        BlockListEntry.objects.create(
            brand=self.brand, kind=BlockListEntry.KIND_DOMAIN, value="*@spam.xyz"
        )
        bad = self.make_ticket(subject="promo", from_email="promo@spam.xyz")
        good = self.make_ticket(subject="real", from_email="buyer@example.com")

        out = StringIO()
        call_command("apply_ignore_gate", "--commit", stdout=out)

        bad.refresh_from_db()
        good.refresh_from_db()
        self.assertTrue(bad.is_ignored)
        self.assertFalse(good.is_ignored)
        self.assertIn(bad.ticket_id, out.getvalue())

    def test_dry_run_does_not_change(self):
        BlockListEntry.objects.create(
            brand=self.brand, kind=BlockListEntry.KIND_DOMAIN, value="*@spam.xyz"
        )
        bad = self.make_ticket(subject="promo", from_email="promo@spam.xyz")
        call_command("apply_ignore_gate", stdout=StringIO())
        bad.refresh_from_db()
        self.assertFalse(bad.is_ignored)


class PendingVisibilityApiTests(TestCase):
    """A verification-failed email (held PendingConversation) must be listable AND openable
    via the API so it is visible in the Inbox + a read-only conversation."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from apps.organizations.models import Organization, Brand
        from apps.tickets.models import PendingConversation
        self.org = Organization.objects.create(name="D")
        self.brand = Brand.objects.create(organization=self.org, name="B")
        self.pending = PendingConversation.objects.create(
            organization=self.org, brand=self.brand,
            customer_email="mayurkhaniya.deodap@gmail.com", subject="damaged product",
            issue_summary="damaged product", body_text="my product is damaged",
            status="awaiting_evidence", original_message_id="<m1@x>", thread_id="<m1@x>",
            evidence_requests=2)
        User = get_user_model()
        self.user = User.objects.create_superuser("admin", "a@b.com", "x")
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_pending_listed(self):
        r = self.api.get("/api/pending/")
        self.assertEqual(r.status_code, 200)
        rows = r.json().get("results", r.json())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["customer_email"], "mayurkhaniya.deodap@gmail.com")

    def test_pending_detail_exposes_body(self):
        r = self.api.get(f"/api/pending/{self.pending.id}/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["body_text"], "my product is damaged")
        self.assertEqual(body["status"], "awaiting_evidence")
        self.assertEqual(body["evidence_requests"], 2)

    def test_closed_pending_hidden_by_default(self):
        # A CLOSED (finished inquiry / promoted) pending must NOT appear in the Pending tab.
        from apps.tickets.models import PendingConversation
        PendingConversation.objects.create(
            organization=self.org, brand=self.brand, customer_email="done@x.com",
            subject="finished inquiry", status="closed")
        rows = self.api.get("/api/pending/").json()
        rows = rows.get("results", rows)
        self.assertEqual(len(rows), 1)                          # only the active one
        self.assertNotIn("done@x.com", [r["customer_email"] for r in rows])
        # ?include_closed=true brings it back.
        allrows = self.api.get("/api/pending/?include_closed=true").json()
        allrows = allrows.get("results", allrows)
        self.assertEqual(len(allrows), 2)


class TicketStatusLifecycleApiTests(TestCase):
    """Agents can change a ticket's status from the detail page: the change persists,
    an activity-log entry (from -> to) is created, and the dashboard counts follow."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from apps.organizations.models import Brand, Mailbox, Organization
        from apps.tickets.models import Ticket
        self.org = Organization.objects.create(name="D")
        self.brand = Brand.objects.create(organization=self.org, name="B")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@d.com")
        self.ticket = Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="c@x.com", subject="help", status=Ticket.STATUS_AWAITING_AGENT)
        User = get_user_model()
        self.user = User.objects.create_superuser("agent1", "a@b.com", "x")
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def _set(self, status):
        return self.api.post(f"/api/tickets/{self.ticket.id}/set-status/",
                             {"status": status}, format="json")

    def test_status_update_api_persists(self):
        from apps.tickets.models import Ticket
        r = self._set(Ticket.STATUS_IN_PROGRESS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], Ticket.STATUS_IN_PROGRESS)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.STATUS_IN_PROGRESS)

    def test_activity_log_created_with_from_to(self):
        from apps.tickets.models import AuditLogEntry, Ticket
        self._set(Ticket.STATUS_IN_PROGRESS)
        log = AuditLogEntry.objects.filter(ticket=self.ticket, event="status_changed").last()
        self.assertIsNotNone(log)
        self.assertEqual(log.actor, "agent1")
        self.assertEqual(log.detail["from"], Ticket.STATUS_AWAITING_AGENT)
        self.assertEqual(log.detail["to"], Ticket.STATUS_IN_PROGRESS)
        self.assertEqual(log.detail["from_label"], "Awaiting Agent")
        self.assertEqual(log.detail["to_label"], "In Progress")

    def test_resolved_then_closed_stamps_resolved_at(self):
        from apps.tickets.models import Ticket
        self._set(Ticket.STATUS_IN_PROGRESS)
        self._set(Ticket.STATUS_RESOLVED)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.STATUS_RESOLVED)
        self.assertIsNotNone(self.ticket.resolved_at)              # terminal -> stamped
        r = self._set(Ticket.STATUS_CLOSED)
        self.assertEqual(r.json()["status"], Ticket.STATUS_CLOSED)

    def test_invalid_status_rejected(self):
        r = self._set("banana")
        self.assertEqual(r.status_code, 400)
        # System-only statuses cannot be set by an agent here.
        self.assertEqual(self._set("auto_resolved").status_code, 400)

    def test_no_op_same_status_logs_nothing(self):
        from apps.tickets.models import AuditLogEntry, Ticket
        r = self._set(Ticket.STATUS_AWAITING_AGENT)               # already this status
        self.assertEqual(r.status_code, 200)
        self.assertFalse(AuditLogEntry.objects.filter(
            ticket=self.ticket, event="status_changed").exists())

    def test_dashboard_counts_refresh(self):
        from apps.analytics import reports
        from apps.tickets.models import Ticket
        before = reports.pipeline_report(Ticket.objects.all())
        self.assertEqual(before["awaiting_agent"], 1)
        self.assertEqual(before["resolved"], 0)
        self._set(Ticket.STATUS_IN_PROGRESS)
        self._set(Ticket.STATUS_RESOLVED)
        after = reports.pipeline_report(Ticket.objects.all())
        self.assertEqual(after["awaiting_agent"], 0)
        self.assertEqual(after["resolved"], 1)                    # counts followed the status
        self._set(Ticket.STATUS_CLOSED)
        closed = reports.pipeline_report(Ticket.objects.all())
        self.assertEqual(closed["closed"], 1)
        self.assertEqual(closed["resolved"], 0)


class OrderOwnerNameRuleTests(TestCase):
    """ORDER OWNER ALWAYS WINS across every surface: the displayed customer name is the
    VERIFIED Shopify order owner, else 'Unknown' -- NEVER the email sender."""

    def setUp(self):
        from apps.organizations.models import Organization, Brand, Mailbox
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")

    def _ticket(self, extracted):
        from apps.tickets.models import Ticket
        # ticket.customer_email is the SENDER address (reply-routing target).
        return Ticket.objects.create(
            organization=self.org, brand=self.brand, mailbox=self.mailbox,
            customer_email="dabhichintan2134@gmail.com", subject="issue", extracted=extracted)

    def _names(self, t):
        from apps.tickets.serializers import _owner_name
        from apps.integrations.care_panel_store import _customer_name
        return _owner_name(t), _customer_name(t)        # (serializer, care-panel)

    def test_priority1_verified_owner_wins(self):
        t = self._ticket({
            "customer_name": "Ronny Misquitta", "customer_name_source": "shopify_verified",
            "customer_email": "ronny@example.com", "phone": "8454094363",
            "sender_name": "Chintan Dabhi", "sender_email": "dabhichintan2134@gmail.com"})
        ser, cp = self._names(t)
        self.assertEqual(ser, "Ronny Misquitta")
        self.assertEqual(cp, "Ronny Misquitta")

    def test_priority2_verified_blank_name_is_unknown(self):
        t = self._ticket({"customer_name_source": "shopify_verified",   # verified, no name
                          "sender_name": "Chintan Dabhi"})
        self.assertEqual(self._names(t), ("Unknown", "Unknown"))

    def test_priority3_verification_failed_is_unknown(self):
        t = self._ticket({"sender_name": "Chintan Dabhi",
                          "sender_email": "dabhichintan2134@gmail.com"})
        self.assertEqual(self._names(t), ("Unknown", "Unknown"))

    def test_nonverified_source_name_never_shown(self):
        # An inquiry/fraud-collected name (source != shopify_verified) is NOT the customer.
        t = self._ticket({"customer_name": "Collected Name", "customer_name_source": "inquiry",
                          "sender_name": "Chintan Dabhi"})
        self.assertEqual(self._names(t), ("Unknown", "Unknown"))

    def test_serializer_exposes_owner_and_sender_separately(self):
        from apps.tickets.serializers import TicketDetailSerializer
        t = self._ticket({
            "customer_name": "Ronny Misquitta", "customer_name_source": "shopify_verified",
            "customer_email": "ronny@example.com", "phone": "8454094363",
            "sender_name": "Chintan Dabhi", "sender_email": "dabhichintan2134@gmail.com"})
        d = TicketDetailSerializer(t).data
        self.assertEqual(d["customer_name"], "Ronny Misquitta")
        self.assertEqual(d["customer_email"], "ronny@example.com")
        self.assertEqual(d["customer_phone"], "8454094363")
        self.assertEqual(d["sender_name"], "Chintan Dabhi")
        self.assertEqual(d["sender_email"], "dabhichintan2134@gmail.com")

    def test_care_panel_payload_uses_owner_name(self):
        from apps.integrations.care_panel_store import _payload
        t = self._ticket({
            "customer_name": "Ronny Misquitta", "customer_name_source": "shopify_verified",
            "customer_email": "ronny@example.com", "phone": "8454094363",
            "sender_name": "Chintan Dabhi", "sender_email": "dabhichintan2134@gmail.com"})
        p = _payload(t)
        self.assertEqual(p["name"], "Ronny Misquitta")
        self.assertNotEqual(p["name"], "Chintan Dabhi")
