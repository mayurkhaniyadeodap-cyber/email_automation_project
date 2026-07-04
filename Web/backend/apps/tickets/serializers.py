from rest_framework import serializers  # type: ignore[import]

from .models import AuditLogEntry, Message, Ticket


# ORDER OWNER ALWAYS WINS: the ticket's customer identity shown in the panel is the verified
# Shopify order owner (name / email / phone). The email SENDER is exposed separately and used
# only for conversation history + reply routing.
def _owner_name(obj):
    # Only the VERIFIED Shopify order owner's name is shown; a blank/missing name, a failed
    # verification, or any other source (e.g. an inquiry/fraud-collected name) -> "Unknown".
    # The email sender's name is NEVER shown (matches care_panel_store._customer_name).
    ex = obj.extracted or {}
    if ex.get("customer_name") and ex.get("customer_name_source") == "shopify_verified":
        return ex["customer_name"]
    return "Unknown"


def _owner_email(obj):
    ex = obj.extracted or {}
    return ex.get("customer_email") or obj.customer_email or ""


def _owner_phone(obj):
    return (obj.extracted or {}).get("phone") or ""


def _sender_email(obj):
    # The actual person who emailed us (reply routing target). ticket.customer_email IS the
    # sender; extracted.sender_email mirrors it.
    return (obj.extracted or {}).get("sender_email") or obj.customer_email or ""


def _sender_name(obj):
    return (obj.extracted or {}).get("sender_name") or ""


class _OwnerSenderFieldsMixin(serializers.Serializer):
    """Adds customer_* (order owner) + sender_* (email sender) method fields. Extends
    Serializer so DRF's metaclass collects these as declared fields on subclasses."""
    customer_name = serializers.SerializerMethodField()
    customer_email = serializers.SerializerMethodField()
    customer_phone = serializers.SerializerMethodField()
    sender_name = serializers.SerializerMethodField()
    sender_email = serializers.SerializerMethodField()

    def get_customer_name(self, obj):
        return _owner_name(obj)

    def get_customer_email(self, obj):
        return _owner_email(obj)

    def get_customer_phone(self, obj):
        return _owner_phone(obj)

    def get_sender_name(self, obj):
        return _sender_name(obj)

    def get_sender_email(self, obj):
        return _sender_email(obj)


class MessageSerializer(serializers.ModelSerializer):
    direction_display = serializers.CharField(
        source="get_direction_display", read_only=True
    )

    class Meta:
        model = Message
        fields = [
            "id", "ticket", "direction", "direction_display", "gmail_message_id",
            "in_reply_to", "references", "from_email", "to_email", "subject",
            "body_text", "body_html", "headers", "attachments", "is_draft",
            "sent_at", "created_at",
        ]


class AuditLogEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLogEntry
        fields = ["id", "ticket", "actor", "event", "detail", "created_at"]


class TicketListSerializer(_OwnerSenderFieldsMixin, serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    priority_display = serializers.CharField(
        source="get_priority_display", read_only=True
    )
    order_id = serializers.SerializerMethodField()
    phone = serializers.SerializerMethodField()
    evidence_requests = serializers.SerializerMethodField()
    # Gmail-style Inbox row: the latest message's sender + a short preview, and unread state.
    last_from = serializers.SerializerMethodField()
    last_preview = serializers.SerializerMethodField()

    def _latest_message(self, obj):
        # obj.messages is prefetched by the viewset -> no extra query per row.
        msgs = [m for m in obj.messages.all() if not m.is_draft]
        return max(msgs, key=lambda m: m.created_at) if msgs else None

    def get_last_from(self, obj):
        m = self._latest_message(obj)
        if m is None:
            return obj.customer_email or ""
        # For an outbound (agent) message show "You"; for inbound show the customer's address.
        return "You" if m.direction == "outbound" else (m.from_email or obj.customer_email or "")

    def get_last_preview(self, obj):
        m = self._latest_message(obj)
        text = ((m.body_text if m else "") or obj.subject or "").strip()
        text = " ".join(text.split())              # collapse whitespace/newlines
        return text[:140]

    def get_order_id(self, obj):
        return (obj.extracted or {}).get("order_id") or ""

    def get_phone(self, obj):
        return (obj.extracted or {}).get("phone") or ""

    def get_evidence_requests(self, obj):
        # How many times we asked this customer for evidence (counts the prefetched
        # audit log -> no extra query per row).
        return sum(1 for a in obj.audit_log.all()
                   if a.event in ("evidence_requested", "evidence_received"))

    class Meta:
        model = Ticket
        fields = [
            "id", "ticket_id", "ticket_number", "brand", "mailbox", "customer_name",
            "customer_email", "customer_phone", "sender_name", "sender_email", "subject",
            "category", "sub_topic", "status", "status_display", "priority",
            "priority_display", "ai_confidence", "ai_handled", "is_ignored",
            "ignored_reason", "order_id", "phone", "evidence_requests",
            "sla_due_at", "created_at", "updated_at",
            "last_activity_at", "agent_unread", "last_from", "last_preview",
        ]


class TicketDetailSerializer(_OwnerSenderFieldsMixin, serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    priority_display = serializers.CharField(
        source="get_priority_display", read_only=True
    )
    messages = MessageSerializer(many=True, read_only=True)
    audit_log = AuditLogEntrySerializer(many=True, read_only=True)
    # ADDITIVE, backward-compatible: the complete email thread (customer <-> DeoDap Support) in
    # chronological order, each entry with sender name/type, email, datetime, body and its
    # attachments. Built from the ticket's linked messages -- new replies appear automatically.
    conversation = serializers.SerializerMethodField()

    def get_conversation(self, obj):
        from apps.ingestion.service import _clean_reply    # strips quoted 'On ... wrote:' history

        # Customer name = the Shopify-VERIFIED order owner; fall back to the email username ONLY
        # when there is no verified order -- never the Gmail sender display name / From header.
        ex = obj.extracted or {}
        cust_name = (ex["customer_name"] if ex.get("customer_name")
                     and ex.get("customer_name_source") == "shopify_verified"
                     else (obj.customer_email or "Customer").split("@")[0])
        convo = []
        for m in obj.messages.all().order_by("created_at"):
            if m.is_draft:
                continue                                   # unsent drafts stay internal
            inbound = m.direction == Message.DIRECTION_INBOUND
            atts = [{
                "id": a.id, "filename": a.filename,
                "content_type": a.content_type or "",
                "url": f"/api/attachments/{a.id}/",
            } for a in m.stored_attachments.all().order_by("created_at")]
            convo.append({
                "sender_name": cust_name if inbound else "DeoDap Support",
                "sender_type": "Customer" if inbound else "DeoDap Support",
                "email": m.from_email or "",
                "subject": (m.subject or "").strip(),
                "datetime": m.sent_at or m.created_at,
                "body": _clean_reply(m.body_text or "").strip(),   # only newly written content
                "attachments": atts,
            })
        return convo

    class Meta:
        model = Ticket
        fields = [
            "id", "ticket_id", "ticket_number", "organization", "brand", "mailbox", "thread_id",
            "customer_name", "customer_email", "customer_phone", "sender_name",
            "sender_email", "subject", "category", "sub_topic", "category_ref",
            "sub_topic_ref", "action_taken", "status", "status_display", "priority",
            "priority_display", "ai_confidence", "ai_handled", "language",
            "sentiment", "mandatory_inputs", "extracted", "is_ignored",
            "ignored_reason", "sla_due_at", "messages", "audit_log", "conversation",
            "created_at", "updated_at",
        ]
        read_only_fields = ["ticket_id"]


from .models import Escalation  # noqa: E402


class EscalationSerializer(serializers.ModelSerializer):
    """Read view for the HIGH-priority manual-review queue + the agent actions on it."""

    ticket_id = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Escalation
        fields = [
            "id", "sender", "sender_name", "subject", "body", "matched_keyword", "status",
            "status_display", "priority", "queue", "message_id", "thread_ids", "received_at",
            "created_at", "resolved_at", "resolved_by", "conversation", "timeline", "attachments",
            "is_read", "assigned_to", "assigned_at", "draft", "ticket_id", "brand",
        ]
        read_only_fields = fields

    def get_ticket_id(self, obj):
        return obj.ticket.ticket_id if obj.ticket_id else None


from .models import InternalEmail  # noqa: E402


class InternalEmailSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = InternalEmail
        fields = [
            "id", "sender", "sender_name", "to_addrs", "matched_recipient", "subject", "body",
            "status", "status_display", "priority", "message_id", "received_at", "created_at",
            "is_read", "assigned_to", "assigned_at", "draft", "conversation", "timeline",
            "attachments", "brand",
        ]
        read_only_fields = fields


from .models import ComposedEmail  # noqa: E402


class ComposedEmailSerializer(serializers.ModelSerializer):
    """Read view for a Compose-page email (draft or sent), with its downloadable attachments."""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    attachments = serializers.SerializerMethodField()

    def get_attachments(self, obj):
        return [{
            "id": a.id, "filename": a.filename,
            "content_type": a.content_type or "",
            "size": a.size, "url": f"/api/attachments/{a.id}/",
        } for a in obj.attachments.all().order_by("created_at")]

    class Meta:
        model = ComposedEmail
        fields = [
            "id", "brand", "from_email", "to_addrs", "cc", "bcc", "subject",
            "body_html", "body_text", "status", "status_display", "message_id",
            "error", "created_by", "sent_at", "created_at", "updated_at", "attachments",
            "conversation", "is_read", "ticket",
        ]
        read_only_fields = fields
