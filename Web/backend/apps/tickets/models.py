"""
Ticket store -- the CRM core (doc sections 8 & 11): tickets, statuses, SLA,
messages (full thread), and audit log. Modeled to match the ticket JSON in the doc.
"""

from django.db import models
from django.utils import timezone

from apps.common import TimestampedModel
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, SubTopic


class Ticket(TimestampedModel):
    """A support ticket. Threads on Gmail threadId so replies join the same ticket."""

    # --- Lifecycle statuses (doc section 11) ---
    STATUS_NEW = "new"
    STATUS_CLASSIFIED = "classified"
    STATUS_AUTO_RESOLVED = "auto_resolved"
    STATUS_AWAITING_EVIDENCE = "awaiting_evidence"
    STATUS_AWAITING_AGENT = "awaiting_agent"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_ESCALATED = "escalated"
    STATUS_RESOLVED = "resolved"
    STATUS_CLOSED = "closed"
    STATUS_IGNORED = "ignored"
    STATUS_CHOICES = [
        (STATUS_NEW, "New"),
        (STATUS_CLASSIFIED, "Classified"),
        (STATUS_AUTO_RESOLVED, "Auto-Resolved"),
        (STATUS_AWAITING_EVIDENCE, "Awaiting Evidence"),
        (STATUS_AWAITING_AGENT, "Awaiting Agent"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_ESCALATED, "Escalated"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_CLOSED, "Closed"),
        (STATUS_IGNORED, "Ignored"),
    ]

    # --- Priority (doc section 12) ---
    PRIORITY_HIGH = "high"
    PRIORITY_NORMAL = "normal"
    PRIORITY_LOW = "low"
    PRIORITY_CHOICES = [
        (PRIORITY_HIGH, "High"),
        (PRIORITY_NORMAL, "Normal"),
        (PRIORITY_LOW, "Low"),
    ]

    ticket_id = models.CharField(max_length=40, unique=True, db_index=True)

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="tickets"
    )
    brand = models.ForeignKey(
        Brand, on_delete=models.CASCADE, related_name="tickets"
    )
    mailbox = models.ForeignKey(
        Mailbox, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tickets",
    )

    # Gmail threading / customer
    thread_id = models.CharField(max_length=255, db_index=True, blank=True, default="")
    customer_email = models.EmailField(blank=True, default="")
    subject = models.CharField(max_length=500, blank=True, default="")

    # Classification snapshot (strings, so taxonomy edits don't rewrite history)
    category = models.CharField(max_length=200, blank=True, default="")
    sub_topic = models.CharField(max_length=255, blank=True, default="")
    category_ref = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    sub_topic_ref = models.ForeignKey(
        SubTopic, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )

    # One-line AI summary of the customer's issue (used for similarity matching).
    issue_summary = models.TextField(blank=True, default="")

    # Set from the external Care Panel store-json response: the customer-facing
    # status link (https://care.deodap.in/t?id=...) and the panel's ticket number.
    tracking_url = models.URLField(max_length=500, blank=True, default="")
    ticket_number = models.CharField(max_length=60, blank=True, default="")

    action_taken = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(
        max_length=30, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True
    )
    priority = models.CharField(
        max_length=10, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL, db_index=True
    )

    # --- AI classification lifecycle ---
    CLS_PENDING = "PENDING_AI"
    CLS_PROCESSING = "AI_PROCESSING"
    CLS_CLASSIFIED = "AI_CLASSIFIED"
    CLS_FAILED = "AI_FAILED"
    CLASSIFICATION_STATUS_CHOICES = [
        (CLS_PENDING, "Pending AI"),
        (CLS_PROCESSING, "AI Processing"),
        (CLS_CLASSIFIED, "AI Classified"),
        (CLS_FAILED, "AI Failed"),
    ]
    classification_status = models.CharField(
        max_length=20, choices=CLASSIFICATION_STATUS_CHOICES,
        default=CLS_PENDING, db_index=True,
    )
    ai_error = models.TextField(blank=True, default="")
    ai_attempts = models.PositiveIntegerField(default=0)

    # AI metadata
    ai_confidence = models.FloatField(null=True, blank=True)
    ai_handled = models.BooleanField(default=False)
    language = models.CharField(max_length=10, blank=True, default="")
    sentiment = models.CharField(max_length=30, blank=True, default="")

    mandatory_inputs = models.JSONField(default=list, blank=True)
    extracted = models.JSONField(default=dict, blank=True)

    # Ignore gate (doc section 3)
    is_ignored = models.BooleanField(default=False)
    ignored_reason = models.CharField(max_length=255, blank=True, default="")

    # Smart Ticket Mgmt: evidence requested but not yet received -> creation of the
    # Gallabox ticket + "created" confirmation is deferred until evidence arrives.
    pending_evidence = models.BooleanField(default=False)

    sla_due_at = models.DateTimeField(null=True, blank=True)
    # Stamped when the ticket first reaches a terminal status (for SLA analytics).
    resolved_at = models.DateTimeField(null=True, blank=True)

    # Gmail-style Inbox ordering: the timestamp of the LATEST message on the thread (inbound or
    # outbound), NOT the ticket's creation time. Bumped by Message.save() so a customer reply moves
    # the conversation to the top of the Inbox. `agent_unread` flips True on a new INBOUND message
    # and is cleared when an agent opens the ticket (the bold/unread row + dot).
    last_activity_at = models.DateTimeField(null=True, blank=True, db_index=True)
    agent_unread = models.BooleanField(default=False, db_index=True)

    # Statuses that count as "closed out" for SLA / analytics.
    TERMINAL_STATUSES = (STATUS_RESOLVED, STATUS_CLOSED, STATUS_AUTO_RESOLVED)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["brand", "status"]),
            models.Index(fields=["brand", "is_ignored"]),
            models.Index(fields=["brand", "last_activity_at"]),
        ]

    def __str__(self):
        return f"{self.ticket_id} ({self.get_status_display()})"

    @classmethod
    def generate_ticket_id(cls):
        """Human ticket id like TKT-2026-000123."""
        year = timezone.now().year
        prefix = f"TKT-{year}-"
        last = (
            cls.objects.filter(ticket_id__startswith=prefix)
            .order_by("-ticket_id")
            .first()
        )
        seq = 1
        if last:
            try:
                seq = int(last.ticket_id.rsplit("-", 1)[-1]) + 1
            except (ValueError, IndexError):
                seq = cls.objects.filter(ticket_id__startswith=prefix).count() + 1
        return f"{prefix}{seq:06d}"

    def save(self, *args, **kwargs):
        if not self.ticket_id:
            self.ticket_id = self.generate_ticket_id()
        # Seed last_activity_at on first save so every ticket sorts sensibly in the Inbox even
        # before its first reply (Message.save bumps it thereafter).
        if self.last_activity_at is None:
            self.last_activity_at = timezone.now()

        # Stamp / clear resolved_at as the ticket enters or leaves a terminal status.
        is_terminal = self.status in self.TERMINAL_STATUSES
        changed_resolved = False
        if is_terminal and self.resolved_at is None:
            self.resolved_at = timezone.now()
            changed_resolved = True
        elif not is_terminal and self.resolved_at is not None:
            self.resolved_at = None
            changed_resolved = True
        # Honor update_fields callers by including resolved_at when we touched it.
        update_fields = kwargs.get("update_fields")
        if changed_resolved and update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"resolved_at"}

        super().save(*args, **kwargs)


class Message(TimestampedModel):
    """One mail in a ticket thread. Dedup on gmail_message_id (Message-ID)."""

    DIRECTION_INBOUND = "inbound"
    DIRECTION_OUTBOUND = "outbound"
    DIRECTION_CHOICES = [
        (DIRECTION_INBOUND, "Inbound (from customer)"),
        (DIRECTION_OUTBOUND, "Outbound (reply)"),
    ]

    ticket = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, related_name="messages"
    )
    direction = models.CharField(
        max_length=10, choices=DIRECTION_CHOICES, default=DIRECTION_INBOUND
    )

    # Gmail identifiers (doc section 2) -- Message-ID unique for dedup.
    gmail_message_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True
    )
    # IMAP UID (per-mailbox) for UID-based dedup / incremental fetch.
    imap_uid = models.BigIntegerField(null=True, blank=True, db_index=True)
    in_reply_to = models.CharField(max_length=255, blank=True, default="")
    references = models.JSONField(default=list, blank=True)

    from_email = models.EmailField(blank=True, default="")
    to_email = models.CharField(max_length=500, blank=True, default="")
    subject = models.CharField(max_length=500, blank=True, default="")
    body_text = models.TextField(blank=True, default="")
    body_html = models.TextField(blank=True, default="")
    headers = models.JSONField(default=dict, blank=True)
    attachments = models.JSONField(default=list, blank=True)

    # An outbound message can sit as a draft awaiting agent approval (Hybrid mode).
    is_draft = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        # Gmail behaviour: a new message on a thread moves that conversation to the TOP of the
        # Inbox (ordered by last_activity_at) -- no new ticket/row. A new INBOUND (customer)
        # message also marks the ticket unread for the agent. Drafts don't count as activity.
        # A direct queryset .update() avoids a recursive Ticket.save() and is cheap.
        if is_new and not self.is_draft and self.ticket_id:
            fields = {"last_activity_at": timezone.now()}
            if self.direction == self.DIRECTION_INBOUND:
                fields["agent_unread"] = True
            Ticket.objects.filter(pk=self.ticket_id).update(**fields)

    def __str__(self):
        return f"{self.ticket.ticket_id} :: {self.get_direction_display()}"


class Attachment(TimestampedModel):
    """A file (image / video / document) from an email, stored on disk and linked
    to its ticket + message so the agent panel can preview and download it."""

    ticket = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, related_name="attachments",
        null=True, blank=True,
    )
    # Evidence received while still a pending conversation (no ticket yet). Moved to
    # `ticket` when the conversation is promoted.
    pending = models.ForeignKey(
        "PendingConversation", on_delete=models.CASCADE, null=True, blank=True,
        related_name="attachments",
    )
    # An attachment an agent sent on an escalation reply (no ticket required).
    escalation = models.ForeignKey(
        "Escalation", on_delete=models.CASCADE, null=True, blank=True,
        related_name="reply_attachments",
    )
    # An attachment on an internal-communications email / reply (no ticket).
    internal_email = models.ForeignKey(
        "InternalEmail", on_delete=models.CASCADE, null=True, blank=True,
        related_name="email_attachments",
    )
    # An attachment on a Compose-page email (no ticket).
    composed_email = models.ForeignKey(
        "ComposedEmail", on_delete=models.CASCADE, null=True, blank=True,
        related_name="attachments",
    )
    message = models.ForeignKey(
        Message, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stored_attachments",
    )
    filename = models.CharField(max_length=300, default="attachment")
    content_type = models.CharField(max_length=120, blank=True, default="")
    size = models.PositiveBigIntegerField(default=0)
    file = models.FileField(upload_to="attachments/%Y/%m/")
    # SHA-256 of the file bytes -- used to dedupe identical media (a customer's reply
    # often re-attaches the same image) so it uploads to the Care Panel only once.
    sha256 = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # URL returned by the external Care Panel after the media is uploaded there
    # (so it shows under "Media Files" on the customer tracking page).
    remote_url = models.URLField(max_length=600, blank=True, default="")

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        owner = self.ticket.ticket_id if self.ticket_id else f"attachment#{self.pk}"
        return f"{owner} :: {self.filename}"

    @property
    def kind(self):
        ct = (self.content_type or "").lower()
        if ct.startswith("image/"):
            return "image"
        if ct.startswith("video/"):
            return "video"
        return "file"


class PendingConversation(TimestampedModel):
    """A classified support email that REQUIRES evidence the customer hasn't sent yet.

    No Ticket (and no ticket id) is created at this stage -- we only hold the email +
    its classification here and ask the customer for photos/video. When they reply
    with evidence, this is promoted into a real Ticket (Smart Ticket Management).
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="pending_conversations"
    )
    brand = models.ForeignKey(
        Brand, on_delete=models.CASCADE, related_name="pending_conversations"
    )
    mailbox = models.ForeignKey(
        Mailbox, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="pending_conversations",
    )

    customer_email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=30, blank=True, default="")
    order_id = models.CharField(max_length=60, blank=True, default="")
    subject = models.CharField(max_length=500, blank=True, default="")

    # Threading / dedup: the original email's Message-ID, plus the latest message id
    # seen for this conversation (e.g. our evidence-request mail).
    original_message_id = models.CharField(max_length=255, db_index=True, blank=True, default="")
    last_message_id = models.CharField(max_length=255, db_index=True, blank=True, default="")
    thread_id = models.CharField(max_length=255, blank=True, default="")
    in_reply_to = models.CharField(max_length=255, blank=True, default="")
    references = models.JSONField(default=list, blank=True)
    headers = models.JSONField(default=dict, blank=True)
    body_text = models.TextField(blank=True, default="")
    body_html = models.TextField(blank=True, default="")

    # Classification snapshot (re-applied verbatim when promoted to a Ticket).
    category = models.CharField(max_length=200, blank=True, default="")
    sub_topic = models.CharField(max_length=255, blank=True, default="")
    category_ref = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="pending_conversations",
    )
    sub_topic_ref = models.ForeignKey(
        SubTopic, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="pending_conversations",
    )
    issue_summary = models.TextField(blank=True, default="")
    confidence = models.FloatField(null=True, blank=True)
    sentiment = models.CharField(max_length=30, blank=True, default="")
    language = models.CharField(max_length=10, blank=True, default="")
    requires_agent = models.BooleanField(default=False)
    extracted = models.JSONField(default=dict, blank=True)

    evidence_requests = models.PositiveIntegerField(default=0)
    # Holding state: "awaiting_evidence" (photo or video) or "waiting_for_video"
    # (a video is mandatory for this category).
    status = models.CharField(max_length=40, blank=True, default="awaiting_evidence")
    # Accumulated across replies so we never re-ask for what was already sent.
    has_evidence = models.BooleanField(default=False)
    has_video = models.BooleanField(default=False)
    has_photo = models.BooleanField(default=False)
    # Whether this case needs photo/video evidence before a ticket. False for cases
    # held only for identity (M1) or mandatory fields -- so the gate doesn't ask for
    # evidence a non-evidence category never needs. db_default=True gives the COLUMN a
    # database-level default, so an INSERT that omits it (e.g. a not-yet-reloaded server
    # process running the pre-field model) still succeeds instead of hitting NOT NULL.
    requires_evidence = models.BooleanField(default=True, db_default=True)

    # Waiting-state timers (Mail Flow §8): 24h reminder (M7R), 72h auto-close (M7C),
    # reply within 7 days reopens. reminder_sent_at guards against repeat reminders;
    # closed_at anchors the reopen window.
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["brand", "customer_email"])]

    def __str__(self):
        return f"PENDING {self.customer_email} :: {self.subject[:40]}"


class AuditLogEntry(TimestampedModel):
    """who / what / when audit trail for a ticket (doc section 8)."""

    ticket = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, related_name="audit_log"
    )
    actor = models.CharField(
        max_length=120, default="system",
        help_text="username, 'ai', or 'system'",
    )
    event = models.CharField(max_length=120)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name_plural = "audit log entries"

    def __str__(self):
        return f"{self.ticket.ticket_id} :: {self.event}"


class Inquiry(TimestampedModel):
    """A completed (or in-progress) BUSINESS INQUIRY captured by the dedicated inquiry
    workflow -- Franchisee / Dropshipping / Company Profile / Invoice Request / Other.

    Separate from Ticket: an inquiry is NOT a support/complaint case. The multi-step
    collected fields live in `data`; `pending` links the conversation state that produced it.
    """

    TYPE_FRANCHISEE = "FRANCHISEE"
    TYPE_DROPSHIPPING = "DROPSHIPPING"
    TYPE_COMPANY_PROFILE = "COMPANY_PROFILE"
    TYPE_INVOICE_REQUEST = "INVOICE_REQUEST"
    TYPE_OTHER = "OTHER_INQUIRY"
    TYPE_CHOICES = [
        (TYPE_FRANCHISEE, "Franchisee"),
        (TYPE_DROPSHIPPING, "Dropshipping"),
        (TYPE_COMPANY_PROFILE, "Company Profile"),
        (TYPE_INVOICE_REQUEST, "Invoice Request"),
        (TYPE_OTHER, "Other Inquiry"),
    ]

    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_CHOICES = [
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_COMPLETED, "Completed"),
    ]

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="inquiries")
    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="inquiries")
    mailbox = models.ForeignKey(
        Mailbox, on_delete=models.SET_NULL, null=True, blank=True, related_name="inquiries")
    pending = models.ForeignKey(
        "PendingConversation", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="inquiries")

    inquiry_type = models.CharField(max_length=30, choices=TYPE_CHOICES, db_index=True)
    channel = models.CharField(max_length=20, default="email")   # email / whatsapp
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                              default=STATUS_IN_PROGRESS, db_index=True)
    queue = models.CharField(max_length=40, blank=True, default="")  # e.g. invoice_team

    # The actual SENDER (inquiries are never order-verified, so this is who wrote in).
    customer_email = models.EmailField(blank=True, default="")
    customer_name = models.CharField(max_length=200, blank=True, default="")
    phone = models.CharField(max_length=30, blank=True, default="")

    # All collected step fields (franchise_city / dropshipping_name / invoice_gst_number ...).
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "inquiries"
        indexes = [models.Index(fields=["brand", "inquiry_type", "status"])]

    def __str__(self):
        return f"INQUIRY[{self.inquiry_type}] {self.customer_email}"


class Escalation(TimestampedModel):
    """High-priority escalation: a legal / consumer-court / grievance / negative-review email.
    When a keyword is detected, ALL automation stops (no classification, verification, tracking,
    evidence, auto-reply, or ticket) and the email lands here as MANUAL_REVIEW_REQUIRED for an
    agent. The customer receives NO automatic email; a ticket is created only if an agent acts.
    """

    STATUS_MANUAL_REVIEW = "manual_review_required"
    STATUS_AWAITING_REPLY = "awaiting_customer_reply"
    STATUS_PENDING = "pending"
    STATUS_TICKET_CREATED = "ticket_created"
    STATUS_RESOLVED = "resolved"
    STATUS_IGNORED = "ignored"
    STATUS_CHOICES = [
        (STATUS_MANUAL_REVIEW, "Manual Review Required"),
        (STATUS_AWAITING_REPLY, "Awaiting Customer Reply"),
        (STATUS_PENDING, "Pending"),
        (STATUS_TICKET_CREATED, "Converted to Ticket"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_IGNORED, "Ignored"),
    ]
    TERMINAL_STATUSES = [STATUS_RESOLVED, STATUS_IGNORED]

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="escalations")
    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="escalations")
    mailbox = models.ForeignKey(
        Mailbox, on_delete=models.SET_NULL, null=True, blank=True, related_name="escalations")

    sender = models.EmailField(blank=True, default="")
    subject = models.CharField(max_length=500, blank=True, default="")
    body = models.TextField(blank=True, default="")
    matched_keyword = models.CharField(max_length=60, blank=True, default="")
    message_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    received_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=30, choices=STATUS_CHOICES,
                              default=STATUS_MANUAL_REVIEW, db_index=True)
    priority = models.CharField(max_length=10, default="high")
    queue = models.CharField(max_length=40, default="escalation", db_index=True)

    # Reply thread: the agent replies to the customer (preserving In-Reply-To / References /
    # Message-ID); the customer's replies continue in THIS escalation. `conversation` is the
    # ordered thread [{direction, body, message_id, in_reply_to, at, agent}]; `thread_ids` are
    # every Message-ID in the thread (for matching the customer's reply back to this record).
    # conversation: ordered thread [{direction: inbound|outbound|note, body, body_html, message_id,
    # in_reply_to, at, agent, attachments}]. Internal NOTES (direction='note') are Care-Panel-only,
    # never emailed. timeline: activity history [{at, event, detail, actor}].
    conversation = models.JSONField(default=list, blank=True)
    thread_ids = models.JSONField(default=list, blank=True)
    references = models.JSONField(default=list, blank=True)
    timeline = models.JSONField(default=list, blank=True)
    attachments = models.JSONField(default=list, blank=True)   # [{filename, url, content_type}]

    # Helpdesk-inbox fields.
    sender_name = models.CharField(max_length=200, blank=True, default="")
    is_read = models.BooleanField(default=False, db_index=True)
    assigned_to = models.CharField(max_length=120, blank=True, default="", db_index=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    draft = models.TextField(blank=True, default="")

    # Set when an agent converts the escalation into a real ticket.
    ticket = models.ForeignKey("Ticket", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="escalations")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.CharField(max_length=120, blank=True, default="")

    def add_event(self, event, *, actor="system", **detail):
        """Append an activity-timeline entry (received / assigned / note / reply / customer reply
        / ticket / resolved / ignored). Caller saves the row."""
        from django.utils import timezone as _tz
        self.timeline = list(self.timeline or []) + [
            {"at": _tz.now().isoformat(), "event": event, "actor": actor, "detail": detail}]

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["brand", "status", "queue"])]

    def __str__(self):
        return f"ESCALATION[{self.matched_keyword}] {self.sender}"


class InternalEmail(TimestampedModel):
    """An INTERNAL company email (sent TO/Cc/Bcc an internal address). Completely independent of
    the customer-support pipeline: NO ticket, auto-reply, escalation, verification, tracking or
    evidence ever runs. It lands in the Internal Communications inbox for an employee to handle.
    """

    STATUS_INTERNAL_REVIEW = "internal_review"
    STATUS_AWAITING_REPLY = "awaiting_reply"
    STATUS_ARCHIVED = "archived"
    STATUS_DELETED = "deleted"
    STATUS_CHOICES = [
        (STATUS_INTERNAL_REVIEW, "Internal Review"),
        (STATUS_AWAITING_REPLY, "Awaiting Reply"),
        (STATUS_ARCHIVED, "Archived"),
        (STATUS_DELETED, "Deleted"),
    ]
    TERMINAL_STATUSES = [STATUS_ARCHIVED, STATUS_DELETED]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="internal_emails")
    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="internal_emails")
    mailbox = models.ForeignKey(Mailbox, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="internal_emails")

    sender = models.EmailField(blank=True, default="")
    sender_name = models.CharField(max_length=200, blank=True, default="")
    to_addrs = models.JSONField(default=list, blank=True)      # the matched internal recipients
    matched_recipient = models.CharField(max_length=255, blank=True, default="")
    subject = models.CharField(max_length=500, blank=True, default="")
    body = models.TextField(blank=True, default="")
    message_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    received_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=30, choices=STATUS_CHOICES,
                              default=STATUS_INTERNAL_REVIEW, db_index=True)
    priority = models.CharField(max_length=10, default="normal")
    is_read = models.BooleanField(default=False, db_index=True)
    assigned_to = models.CharField(max_length=120, blank=True, default="", db_index=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    draft = models.TextField(blank=True, default="")

    conversation = models.JSONField(default=list, blank=True)
    thread_ids = models.JSONField(default=list, blank=True)
    references = models.JSONField(default=list, blank=True)
    timeline = models.JSONField(default=list, blank=True)
    attachments = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["brand", "status"])]

    def add_event(self, event, *, actor="system", **detail):
        from django.utils import timezone as _tz
        self.timeline = list(self.timeline or []) + [
            {"at": _tz.now().isoformat(), "event": event, "actor": actor, "detail": detail}]

    def __str__(self):
        return f"INTERNAL[{self.matched_recipient}] {self.sender}"


class ComposedEmail(TimestampedModel):
    """A NEW email an agent composes and sends from the Compose page (Gmail-like). Completely
    standalone: it NEVER creates or updates a ticket, escalation, pending or internal email. It
    only records the message the agent wrote (draft or sent) so there is a history of outbound
    mail. Sent via the SAME SMTP sender as every other outbound email (ingestion.send_composed_email
    -> smtp_client.send_email); the chosen From is a validated brand SupportEmail alias."""

    STATUS_DRAFT = "draft"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_SENT, "Sent"),
        (STATUS_FAILED, "Failed"),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE,
                                     related_name="composed_emails")
    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="composed_emails")
    mailbox = models.ForeignKey(Mailbox, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="composed_emails")

    from_email = models.CharField(max_length=255, blank=True, default="")   # chosen alias
    to_addrs = models.TextField(blank=True, default="")                     # comma/space separated
    cc = models.TextField(blank=True, default="")
    bcc = models.TextField(blank=True, default="")
    subject = models.CharField(max_length=500, blank=True, default="")
    body_html = models.TextField(blank=True, default="")
    body_text = models.TextField(blank=True, default="")

    status = models.CharField(max_length=10, choices=STATUS_CHOICES,
                              default=STATUS_DRAFT, db_index=True)
    message_id = models.CharField(max_length=255, blank=True, default="")   # first outbound Message-ID
    error = models.TextField(blank=True, default="")                        # last send error
    created_by = models.CharField(max_length=150, blank=True, default="")   # agent
    sent_at = models.DateTimeField(null=True, blank=True)

    # Gmail-style threading. `conversation` is the ordered thread
    # [{direction: outbound|inbound, from, to, subject, body_html, body_text, message_id,
    #   in_reply_to, at, agent, attachments:[{filename,url,content_type}]}].
    # `thread_refs` is a space-joined string of EVERY RFC Message-ID in the thread (outbound +
    # inbound) -- an incoming reply is matched to this thread when its In-Reply-To / References
    # contains any of these ids (SQLite-friendly LIKE lookup). `is_read` flips to False when a
    # customer reply arrives. Optional `ticket` link if this thread is ever tied to a ticket.
    conversation = models.JSONField(default=list, blank=True)
    thread_refs = models.TextField(blank=True, default="")
    is_read = models.BooleanField(default=True, db_index=True)
    ticket = models.ForeignKey("Ticket", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="composed_emails")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["brand", "status"])]

    def add_message(self, entry):
        """Append a thread entry and register its Message-ID for reply matching. Caller saves."""
        self.conversation = list(self.conversation or []) + [entry]
        mid = (entry.get("message_id") or "").strip()
        if mid and mid not in (self.thread_refs or ""):
            self.thread_refs = f"{self.thread_refs} {mid}".strip()

    def __str__(self):
        return f"COMPOSED[{self.status}] {self.to_addrs}"


class ProcessedEmail(TimestampedModel):
    """Idempotency guard for INBOUND email. One row per incoming Gmail/IMAP Message-ID we have
    started handling, so the SAME email is processed -- and auto-replied -- EXACTLY once, across
    re-polls AND concurrent background workers.

    The UNIQUE `message_id` is the atomic cross-worker lock: the first worker to INSERT the row
    wins and proceeds; a concurrent or re-delivered copy hits the unique constraint and skips
    safely. This closes the gap where a re-fetched / raced PENDING REPLY (which creates no Ticket
    or Message row) re-sent its auto-reply. `created_at` = when we claimed it; `completed_at` is
    stamped after successful handling."""

    message_id = models.CharField(max_length=255, unique=True, db_index=True)
    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, null=True, blank=True,
                                related_name="processed_emails")
    thread_id = models.CharField(max_length=255, blank=True, default="")
    from_email = models.CharField(max_length=255, blank=True, default="")
    auto_reply_sent = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"PROCESSED[{self.message_id}]"
