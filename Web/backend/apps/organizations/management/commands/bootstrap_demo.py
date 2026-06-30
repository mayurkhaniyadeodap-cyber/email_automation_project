"""
Bootstrap a runnable demo of the care panel (Phase 0).

Creates: a superuser (admin/admin) for dev, a demo Organization -> Brand ->
Mailbox, BrandSettings, a default Ignore/Block list, the 16-category taxonomy,
and two demo tickets drawn from the roadmap's worked examples (doc section 7).

Usage:  python manage.py bootstrap_demo
"""

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.brand_settings.models import BlockListEntry, BrandSettings
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, SubTopic
from apps.tickets.models import AuditLogEntry, Message, Ticket

User = get_user_model()

DEFAULT_BLOCK_LIST = [
    (BlockListEntry.KIND_DOMAIN, "*@newsletter.xyz", "Marketing newsletters"),
    (BlockListEntry.KIND_NOREPLY, "noreply@", "No-reply senders"),
    (BlockListEntry.KIND_NOREPLY, "no-reply@", "No-reply senders"),
    (BlockListEntry.KIND_NOREPLY, "mailer-daemon@", "Bounce daemon"),
    (BlockListEntry.KIND_NOREPLY, "notifications@", "Automated notifications"),
    (BlockListEntry.KIND_MARKETING, "List-Unsubscribe", "Marketing header present"),
    (BlockListEntry.KIND_MARKETING, "Precedence: bulk", "Bulk mail header"),
    (BlockListEntry.KIND_MARKETING, "Auto-Submitted: auto-generated", "Auto-generated"),
    (BlockListEntry.KIND_SPAM, "Authentication-Results: dmarc=fail", "Failed DMARC"),
]


class Command(BaseCommand):
    help = "Bootstrap a runnable demo org/brand/mailbox + taxonomy + demo tickets."

    def handle(self, *args, **opts):
        # Dev superuser.
        admin, made = User.objects.get_or_create(
            username="admin",
            defaults={"email": "researchanddevelopment@deodap.com", "is_staff": True,
                      "is_superuser": True},
        )
        if made:
            admin.set_password("admin")
            admin.save()
            self.stdout.write(self.style.SUCCESS("Created superuser admin / admin"))

        org, _ = Organization.objects.get_or_create(name="DeoDap")
        org.members.add(admin)
        brand, _ = Brand.objects.get_or_create(organization=org, name="DeoDap.in")
        mailbox, _ = Mailbox.objects.get_or_create(
            email_address="care@deodap.com", defaults={"brand": brand}
        )

        BrandSettings.objects.get_or_create(
            brand=brand,
            defaults={
                "ai_provider": BrandSettings.PROVIDER_GEMINI,
                "confidence_threshold": 0.75,
                "automation_toggles": {
                    "info_only": "auto_send",
                    "await_evidence": "auto_send",
                    "create_ticket": "draft",
                    "update_system": "off",
                    "trigger_cancellation_refund_pickup": "off",
                },
            },
        )

        for kind, value, note in DEFAULT_BLOCK_LIST:
            BlockListEntry.objects.get_or_create(
                brand=brand, kind=kind, value=value, defaults={"note": note}
            )

        # Taxonomy.
        call_command("seed_taxonomy", brand=brand.id)

        # Demo tickets from the worked examples (doc section 7).
        self._make_demo_tickets(org, brand, mailbox)

        self.stdout.write(self.style.SUCCESS(
            "\nDemo ready. Run the server, log in at /admin (admin/admin), "
            "or hit the API at /api/. Get a token via POST /api/auth/token/."
        ))

    def _make_demo_tickets(self, org, brand, mailbox):
        def sub(code):
            return SubTopic.objects.filter(category__brand=brand, code=code).first()

        examples = [
            {
                "subject": "Where is my order DD123, when will it come?",
                "code": "1.1",
                "category": "1. Shipment & Delivery Tracking",
                "action": "Auto-send tracking link (Info only)",
                "status": Ticket.STATUS_AUTO_RESOLVED,
                "priority": Ticket.PRIORITY_LOW,
                "confidence": 0.93,
                "ai_handled": True,
                "extracted": {"order_id": "DD123"},
                "body": "Where is my order DD123, when will it come?",
            },
            {
                "subject": "Order not received",
                "code": "3.1",
                "category": "3. Delivery Issues (Post-Delivery)",
                "action": "Create Ticket (POD investigation)",
                "status": Ticket.STATUS_AWAITING_AGENT,
                "priority": Ticket.PRIORITY_HIGH,
                "confidence": 0.88,
                "ai_handled": False,
                "extracted": {"order_id": "DD123456"},
                "body": "Tracking says delivered 3 days ago but I never got it.",
            },
            {
                "subject": "I got a scam call asking for OTP",
                "code": "16.2",
                "category": "16. Feedback, Support & Fraud",
                "action": "Human only, HIGH priority",
                "status": Ticket.STATUS_ESCALATED,
                "priority": Ticket.PRIORITY_HIGH,
                "confidence": 0.97,
                "ai_handled": False,
                "extracted": {},
                "body": "Someone called pretending to be DeoDap and asked for my OTP.",
            },
        ]

        created = 0
        for ex in examples:
            st = sub(ex["code"])
            if Ticket.objects.filter(brand=brand, subject=ex["subject"]).exists():
                continue
            t = Ticket.objects.create(
                organization=org, brand=brand, mailbox=mailbox,
                thread_id=f"demo_thread_{ex['code'].replace('.', '_')}",
                customer_email="buyer@example.com",
                subject=ex["subject"],
                category=ex["category"],
                sub_topic=f"{st.code} {st.name}" if st else "",
                sub_topic_ref=st,
                category_ref=st.category if st else None,
                action_taken=ex["action"],
                status=ex["status"], priority=ex["priority"],
                ai_confidence=ex["confidence"], ai_handled=ex["ai_handled"],
                mandatory_inputs=st.mandatory_inputs if st else [],
                extracted=ex["extracted"],
                sla_due_at=timezone.now() + timezone.timedelta(hours=4),
            )
            Message.objects.create(
                ticket=t, direction=Message.DIRECTION_INBOUND,
                from_email="buyer@example.com", to_email=mailbox.email_address,
                subject=ex["subject"], body_text=ex["body"],
            )
            AuditLogEntry.objects.create(
                ticket=t, actor="ai", event="classified",
                detail={"category": ex["category"], "confidence": ex["confidence"]},
            )
            created += 1

        self.stdout.write(self.style.SUCCESS(f"Demo tickets created: {created}"))
