import logging

from django.core.files.base import ContentFile
from django.http import FileResponse
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import (
    action,
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.scoping import OrgScopedViewSet

from .models import Attachment, AuditLogEntry, Message, Ticket
from .serializers import (
    AuditLogEntrySerializer,
    MessageSerializer,
    TicketDetailSerializer,
    TicketListSerializer,
)

logger = logging.getLogger(__name__)


def _range_window(params):
    """(since, until) dates from ?range=today|yesterday|7d|30d, or explicit ?since=&until=."""
    from datetime import date, timedelta

    rng = params.get("range")
    today = timezone.now().date()
    if rng == "today":
        return today, today
    if rng == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if rng == "7d":
        return today - timedelta(days=6), today
    if rng == "30d":
        return today - timedelta(days=29), today

    def _d(v):
        try:
            return date.fromisoformat(v)
        except (TypeError, ValueError):
            return None
    return _d(params.get("since")), _d(params.get("until"))


class TicketViewSet(OrgScopedViewSet):
    queryset = Ticket.objects.select_related("brand", "mailbox").prefetch_related(
        "messages", "audit_log"
    )
    org_lookup = "organization"
    brand_lookup = "brand"
    search_fields = ["ticket_id", "subject", "customer_email"]
    ordering_fields = ["created_at", "updated_at", "priority", "sla_due_at"]

    def get_serializer_class(self):
        if self.action == "list":
            return TicketListSerializer
        return TicketDetailSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        # Queue filters (status / priority / Ignored tab) only shape the LIST view.
        # Detail actions must still reach a ticket by id even when it's ignored,
        # otherwise un-ignoring a wrongly-filtered mail would 404 (doc section 3).
        if self.action != "list":
            return qs
        params = self.request.query_params
        status = params.get("status")
        if status == "open":
            # "Open" = active queue: not ignored, not in a terminal status.
            qs = qs.exclude(status__in=Ticket.TERMINAL_STATUSES)
        elif status:
            # Comma list -> any of these statuses (e.g. in_progress,escalated).
            values = [s for s in status.split(",") if s]
            qs = qs.filter(status__in=values) if len(values) > 1 else qs.filter(status=values[0])
        if params.get("priority"):
            qs = qs.filter(priority=params["priority"])
        # Ignored mails live in a separate "Ignored" tab (doc section 3).
        # Default queue hides them unless ?ignored=true (or ?ignored=all).
        ignored = params.get("ignored", "false").lower()
        if ignored == "true":
            qs = qs.filter(is_ignored=True)
        elif ignored != "all":
            qs = qs.filter(is_ignored=False)
        return qs

    @action(detail=True, methods=["post"])
    def reply(self, request, pk=None):
        """
        Manual reply-in-thread (doc section 2, "Sending replies"). Records an
        outbound Message on the thread and, when the mailbox is Gmail-connected,
        actually sends it in-thread via the ingestion service. Drafts are stored
        for agent approval and not sent (Hybrid mode).
        """
        ticket = self.get_object()
        body = request.data.get("body_text", "").strip()
        if not body:
            return Response(
                {"detail": "body_text is required."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        is_draft = bool(request.data.get("is_draft", False))
        # The agent may choose the From (a validated SupportEmail alias); default to the mailbox
        # that received the email.
        from apps.ingestion.service import resolve_sender_email
        default_from = ticket.mailbox.email_address if ticket.mailbox else ""
        sender_email = resolve_sender_email(
            ticket.mailbox, request.data.get("from_email", ""), default=default_from)
        msg = Message.objects.create(
            ticket=ticket,
            direction=Message.DIRECTION_OUTBOUND,
            from_email=sender_email,
            to_email=ticket.customer_email,
            subject=f"Re: {ticket.subject}",
            body_text=body,
            is_draft=is_draft,
            sent_at=None if is_draft else timezone.now(),
        )
        gmail_id = None
        if not is_draft:
            from apps.ingestion import service as ingestion_service

            gmail_id = ingestion_service.send_reply(msg)
            # Attribute the reply to the agent (employee), recording the actual alias From used.
            from apps.analytics.logging import log_manual_reply
            log_manual_reply(
                brand=ticket.brand, employee=request.user, customer_email=ticket.customer_email,
                subject=msg.subject, message_id=gmail_id or "", thread_id=ticket.thread_id,
                ticket=ticket, body=body, sender_email=sender_email)
        AuditLogEntry.objects.create(
            ticket=ticket,
            actor=request.user.get_username(),
            event="draft_created" if is_draft else "reply_sent",
            detail={"message_id": msg.id, "gmail_message_id": gmail_id},
        )
        if not is_draft and ticket.status in (
            Ticket.STATUS_NEW,
            Ticket.STATUS_CLASSIFIED,
            Ticket.STATUS_AWAITING_AGENT,
        ):
            ticket.status = Ticket.STATUS_IN_PROGRESS
            ticket.save(update_fields=["status", "updated_at"])
        return Response(
            MessageSerializer(msg).data, status=http_status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def classify(self, request, pk=None):
        """Run (or re-run) the AI classifier on this ticket (doc section 4).

        Returns the updated ticket. 409 if the brand has no AI provider configured
        (paste a key in Settings first); the ticket is left for an agent.
        """
        from apps.classifier import service as classifier

        ticket = self.get_object()
        result = classifier.classify_ticket(ticket)
        if result is None:
            return Response(
                {"detail": "No AI provider configured for this brand."},
                status=http_status.HTTP_409_CONFLICT,
            )
        ticket.refresh_from_db()
        return Response(TicketDetailSerializer(ticket).data)

    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):
        """Run the decision engine on this ticket (doc section 5).

        Applies the sub-topic's IF/THEN/Action rules in Hybrid mode: auto-sends
        Info-only / evidence requests, drafts Create-Ticket replies, and applies
        the guardrails. Returns the updated ticket.
        """
        from apps.decision import engine
        from apps.integrations import context as live_context

        ticket = self.get_object()
        facts = live_context.build_context(ticket)
        plan = engine.run(ticket, context=facts, actor=request.user.get_username())
        if plan is None:
            return Response(
                {"detail": "Ticket is ignored; nothing to decide."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        ticket.refresh_from_db()
        return Response(TicketDetailSerializer(ticket).data)

    @action(detail=True, methods=["post"])
    def correct(self, request, pk=None):
        """Agent reclassification (doc §13 AI-accuracy ground truth).

        Re-points the ticket at the correct sub-topic and logs a 'correction'
        audit event, which the AI-accuracy report counts against the classifier.
        Body: {"sub_topic_ref": <SubTopic id>}.
        """
        from apps.taxonomy.models import SubTopic

        ticket = self.get_object()
        sub_id = request.data.get("sub_topic_ref") or request.data.get("sub_topic_id")
        if not sub_id:
            return Response(
                {"detail": "sub_topic_ref is required."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        sub = SubTopic.objects.filter(
            pk=sub_id, category__brand=ticket.brand
        ).select_related("category").first()
        if sub is None:
            return Response(
                {"detail": "Unknown sub_topic for this brand."},
                status=http_status.HTTP_404_NOT_FOUND,
            )
        was = {"category": ticket.category, "sub_topic": ticket.sub_topic}
        ticket.category_ref = sub.category
        ticket.sub_topic_ref = sub
        ticket.category = f"{sub.category.code}. {sub.category.name}"
        ticket.sub_topic = f"{sub.code} {sub.name}"
        ticket.mandatory_inputs = sub.mandatory_inputs
        ticket.save(update_fields=[
            "category_ref", "sub_topic_ref", "category", "sub_topic",
            "mandatory_inputs", "updated_at",
        ])
        AuditLogEntry.objects.create(
            ticket=ticket, actor=request.user.get_username(), event="correction",
            detail={"was": was, "now": {"category": ticket.category,
                                        "sub_topic": ticket.sub_topic}},
        )
        return Response(TicketDetailSerializer(ticket).data)

    @action(detail=True, methods=["get"])
    def attachments(self, request, pk=None):
        """List the ticket's attachments (doc §13). Stored files are downloadable
        (with a `url`); attachments we only have metadata for are listed too.
        """
        ticket = self.get_object()
        items = []
        has_photo = has_video = False

        def _kind(ct):
            ct = (ct or "").lower()
            return "image" if ct.startswith("image/") else \
                ("video" if ct.startswith("video/") else "file")

        stored = list(ticket.attachments.all())
        stored_names = {a.filename for a in stored}
        for a in stored:
            ct = (a.content_type or "").lower()
            has_photo = has_photo or ct.startswith("image/")
            has_video = has_video or ct.startswith("video/")
            items.append({
                "id": a.id, "filename": a.filename, "content_type": a.content_type,
                "size": a.size, "kind": a.kind, "created_at": a.created_at,
                "downloadable": True, "url": f"/api/attachments/{a.id}/",
            })
        # Metadata-only attachments (no stored file) from the raw messages.
        for msg in ticket.messages.all():
            for att in msg.attachments or []:
                mime = (att.get("mime_type") or "").lower()
                has_photo = has_photo or mime.startswith("image/")
                has_video = has_video or mime.startswith("video/")
                if att.get("filename") in stored_names:
                    continue
                items.append({
                    "filename": att.get("filename"), "content_type": att.get("mime_type"),
                    "size": att.get("size", 0), "kind": _kind(att.get("mime_type")),
                    "downloadable": False,
                })
        return Response({
            "count": len(items),
            "attachments": items,
            "evidence": {"has_photo": has_photo, "has_unboxing_video": has_video},
        })

    @action(detail=True, methods=["post"])
    def ignore(self, request, pk=None):
        """Manually move a ticket to the Ignored tab (doc section 3).

        For mails the gate let through that an agent decides are junk. Optional
        `reason` is stored and shown in the Ignored tab audit.
        """
        ticket = self.get_object()
        reason = (request.data.get("reason") or "").strip() or "Manually ignored by agent"
        ticket.is_ignored = True
        ticket.ignored_reason = reason
        ticket.status = Ticket.STATUS_IGNORED
        ticket.save(
            update_fields=["is_ignored", "ignored_reason", "status", "updated_at"]
        )
        AuditLogEntry.objects.create(
            ticket=ticket, actor=request.user.get_username(),
            event="ignored", detail={"reason": reason, "manual": True},
        )
        return Response(TicketDetailSerializer(ticket).data)

    @action(detail=True, methods=["post"])
    def unignore(self, request, pk=None):
        """Restore a wrongly-filtered ticket from the Ignored tab to the queue
        (doc section 3, "un-ignore if something was wrongly filtered")."""
        ticket = self.get_object()
        if not ticket.is_ignored:
            return Response(
                {"detail": "Ticket is not ignored."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        previous_reason = ticket.ignored_reason
        ticket.is_ignored = False
        ticket.ignored_reason = ""
        # Back to the queue: classified if the AI already tagged it, else new.
        ticket.status = (
            Ticket.STATUS_CLASSIFIED if ticket.sub_topic_ref else Ticket.STATUS_NEW
        )
        ticket.save(
            update_fields=["is_ignored", "ignored_reason", "status", "updated_at"]
        )
        AuditLogEntry.objects.create(
            ticket=ticket, actor=request.user.get_username(),
            event="unignored", detail={"previous_reason": previous_reason},
        )
        return Response(TicketDetailSerializer(ticket).data)

    # Statuses an AGENT may set from the Ticket Detail page (the lifecycle the spec
    # exposes). System-only statuses (new / classified / auto_resolved / awaiting_evidence
    # / ignored) are NOT agent-settable here -- they are driven by the pipeline.
    AGENT_SETTABLE_STATUSES = (
        Ticket.STATUS_AWAITING_AGENT,
        Ticket.STATUS_IN_PROGRESS,
        Ticket.STATUS_ESCALATED,
        Ticket.STATUS_RESOLVED,
        Ticket.STATUS_CLOSED,
    )

    @action(detail=True, methods=["post"], url_path="set-status")
    def set_status(self, request, pk=None):
        """Agent changes the ticket status (lifecycle: Awaiting Agent -> In Progress ->
        Resolved -> Closed, plus Escalated). Persists Ticket.status and records a
        'status_changed' audit entry (from -> to) for the activity log. Dashboard counts
        derive from Ticket.status, so the next overview fetch reflects the change."""
        ticket = self.get_object()
        target = (request.data.get("status") or "").strip()
        labels = dict(Ticket.STATUS_CHOICES)
        if target not in self.AGENT_SETTABLE_STATUSES:
            return Response(
                {"detail": "status must be one of "
                           f"{list(self.AGENT_SETTABLE_STATUSES)}."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        previous = ticket.status
        if target == previous:
            return Response(TicketDetailSerializer(ticket).data)   # no-op, no log noise
        ticket.status = target
        fields = ["status", "updated_at"]
        # Moving a ticket back into an ACTIVE status restores it from the Ignored tab.
        if ticket.is_ignored:
            ticket.is_ignored = False
            ticket.ignored_reason = ""
            fields += ["is_ignored", "ignored_reason"]
        ticket.save(update_fields=fields)               # save() stamps/clears resolved_at
        AuditLogEntry.objects.create(
            ticket=ticket, actor=request.user.get_username(), event="status_changed",
            detail={"from": previous, "to": target,
                    "from_label": labels.get(previous, previous),
                    "to_label": labels.get(target, target)},
        )
        return Response(TicketDetailSerializer(ticket).data)


class MessageViewSet(OrgScopedViewSet):
    serializer_class = MessageSerializer
    queryset = Message.objects.select_related("ticket")
    org_lookup = "ticket__organization"
    brand_lookup = "ticket__brand"

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("ticket"):
            qs = qs.filter(ticket=self.request.query_params["ticket"])
        return qs


class AuditLogEntryViewSet(OrgScopedViewSet):
    serializer_class = AuditLogEntrySerializer
    queryset = AuditLogEntry.objects.select_related("ticket")
    org_lookup = "ticket__organization"
    brand_lookup = "ticket__brand"
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("ticket"):
            qs = qs.filter(ticket=self.request.query_params["ticket"])
        return qs


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def attachment_file(request, pk):
    """Serve an attachment file for inline preview (image/video) or download.

    Auth accepts the DRF token in the Authorization header OR a ?token= query param
    (so <img>/<video> tags, which can't send headers, still work). Org-scoped.
    """
    key = request.query_params.get("token") or ""
    if not key:
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if header.startswith("Token "):
            key = header.split(" ", 1)[1]
    tok = Token.objects.select_related("user").filter(key=key).first() if key else None
    user = tok.user if tok else None
    if user is None:
        return Response({"detail": "Authentication required."},
                        status=http_status.HTTP_401_UNAUTHORIZED)

    att = Attachment.objects.select_related(
        "ticket", "escalation", "internal_email").filter(pk=pk).first()
    if att is None:
        return Response(status=http_status.HTTP_404_NOT_FOUND)
    org_id = (att.ticket and att.ticket.organization_id) or (
        att.escalation and att.escalation.organization_id) or (
        att.internal_email and att.internal_email.organization_id)
    if not user.is_superuser and not (org_id and user.organizations.filter(pk=org_id).exists()):
        return Response({"detail": "Forbidden."}, status=http_status.HTTP_403_FORBIDDEN)

    try:
        handle = att.file.open("rb")
    except FileNotFoundError:
        return Response(status=http_status.HTTP_404_NOT_FOUND)
    resp = FileResponse(handle, content_type=att.content_type or "application/octet-stream")
    disposition = "attachment" if request.query_params.get("download") else "inline"
    resp["Content-Disposition"] = f'{disposition}; filename="{att.filename}"'
    return resp


from .models import Escalation  # noqa: E402
from .serializers import EscalationSerializer  # noqa: E402


class EscalationViewSet(OrgScopedViewSet):
    """The HIGH-priority manual-review queue, driven like a helpdesk inbox. Listing + the detail
    view (retrieve marks read) + agent actions: reply / note / assign / pending / draft /
    create_ticket / resolve / ignore. No automation ever touches these records."""

    queryset = Escalation.objects.select_related("brand", "ticket").all()
    serializer_class = EscalationSerializer
    org_lookup = "organization"
    brand_lookup = "brand"
    search_fields = ["sender", "sender_name", "subject", "matched_keyword", "body", "assigned_to"]
    ordering_fields = ["created_at", "received_at", "status"]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params
        for field in ("status", "priority", "matched_keyword", "assigned_to"):
            if p.get(field):
                qs = qs.filter(**{field: p[field]})
        # Date range filter (?range=today|yesterday|7d|30d, or ?since=&until=).
        since, until = _range_window(p)
        if since:
            qs = qs.filter(created_at__date__gte=since)
        if until:
            qs = qs.filter(created_at__date__lte=until)
        return qs

    def retrieve(self, request, *args, **kwargs):
        """Opening the detail marks the escalation READ (helpdesk behaviour)."""
        esc = self.get_object()
        if not esc.is_read:
            esc.is_read = True
            esc.save(update_fields=["is_read", "updated_at"])
            logger.info("ESCALATION_OPENED escalation=%s agent=%s", esc.id, self._actor())
        return Response(self.get_serializer(esc).data)

    @action(detail=False, methods=["get"])
    def unread_count(self, request):
        """Lightweight poll endpoint for the sidebar badge + new-item toasts: the count of UNREAD
        escalations in the selected org/brand that still need attention (terminal resolved/ignored
        excluded), plus the newest unread items (id / sender / name / subject) so the UI can show a
        per-item toast. Read-only -- no side effects, never touches the escalation workflow.
        Bypasses the list's status/range filters via the org/brand scope only, so the count is
        independent of whatever filter the list view has applied."""
        base = (OrgScopedViewSet.get_queryset(self)
                .filter(is_read=False)
                .exclude(status__in=Escalation.TERMINAL_STATUSES))
        items = [{"id": e.id, "sender": e.sender, "sender_name": e.sender_name, "subject": e.subject}
                 for e in base.order_by("-created_at")[:25]]
        return Response({"count": base.count(), "items": items})

    def _actor(self):
        u = self.request.user
        return getattr(u, "email", "") or getattr(u, "username", "") or "agent"

    def _log(self, esc, event, **detail):
        if esc.ticket_id:                          # audit trail lives on the linked ticket
            AuditLogEntry.objects.create(ticket=esc.ticket, actor=self._actor(),
                                         event=event, detail=detail)

    @action(detail=True, methods=["post"])
    def reply(self, request, pk=None):
        """Agent replies to the customer in the escalation thread (preserves In-Reply-To /
        References / Message-ID). Status -> Awaiting Customer Reply."""
        from apps.ingestion.service import send_escalation_reply

        esc = self.get_object()
        body = (request.data.get("body") or request.data.get("message") or "").strip()
        subject = (request.data.get("subject") or "").strip()
        if not body:
            return Response({"detail": "Reply body is required."}, status=http_status.HTTP_400_BAD_REQUEST)

        # Multiple file attachments -> store each (so it shows in history + is downloadable) and
        # carry it on the outgoing email.
        email_atts, stored = [], []
        for f in request.FILES.getlist("attachments"):
            content = f.read()
            att = Attachment.objects.create(
                escalation=esc, filename=f.name, content_type=f.content_type or "",
                size=len(content), file=ContentFile(content, name=f.name))
            email_atts.append((f.name, content, f.content_type or "application/octet-stream"))
            stored.append({"filename": f.name, "content_type": f.content_type or "",
                           "url": f"/api/attachments/{att.id}/"})

        sent_id = send_escalation_reply(esc, body, agent=self._actor(), subject=subject,
                                        email_attachments=email_atts, stored_attachments=stored,
                                        from_email=request.data.get("from_email", ""))
        esc.refresh_from_db()
        if not sent_id:
            # The email did NOT actually go out (SMTP/send failure) -- tell the agent instead of
            # silently showing it as sent. The draft + attachments are kept so they can retry.
            data = self.get_serializer(esc).data
            data["send_failed"] = True
            data["detail"] = ("The reply could NOT be emailed (SMTP/send failed). It was saved so "
                              "you can retry. Check the backend SMTP-SEND-FAILED log for the exact "
                              "reason and your SMTP settings.")
            return Response(data, status=http_status.HTTP_502_BAD_GATEWAY)
        from apps.analytics.logging import log_manual_reply
        from apps.ingestion.service import resolve_sender_email
        chosen = resolve_sender_email(esc.mailbox, request.data.get("from_email", ""),
                                      default=(esc.mailbox.email_address if esc.mailbox else ""))
        log_manual_reply(brand=esc.brand, employee=request.user, customer_email=esc.sender,
                         subject=subject or esc.subject, message_id=sent_id, escalation=esc,
                         thread_id=(esc.thread_ids or [""])[0], body=body, attachments=len(stored),
                         sender_email=chosen)
        return Response(self.get_serializer(esc).data)

    @action(detail=True, methods=["post"])
    def note(self, request, pk=None):
        """Add an INTERNAL note -- Care-Panel-only, never emailed to the customer."""
        from apps.ingestion.service import add_escalation_note

        esc = self.get_object()
        text = (request.data.get("note") or request.data.get("body") or "").strip()
        if not text:
            return Response({"detail": "Note is required."}, status=http_status.HTTP_400_BAD_REQUEST)
        add_escalation_note(esc, text, agent=self._actor())
        esc.refresh_from_db()
        return Response(self.get_serializer(esc).data)

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        esc = self.get_object()
        agent = (request.data.get("assigned_to") or request.data.get("agent") or "").strip()
        if not agent:
            return Response({"detail": "assigned_to is required."}, status=http_status.HTTP_400_BAD_REQUEST)
        esc.assigned_to = agent
        esc.assigned_at = timezone.now()
        esc.add_event("assigned", actor=self._actor(), assigned_to=agent)
        esc.save(update_fields=["assigned_to", "assigned_at", "timeline", "updated_at"])
        logger.info("ESCALATION_ASSIGNED escalation=%s to=%s by=%s", esc.id, agent, self._actor())
        return Response(self.get_serializer(esc).data)

    @action(detail=True, methods=["post"])
    def pending(self, request, pk=None):
        esc = self.get_object()
        esc.status = Escalation.STATUS_PENDING
        esc.add_event("marked_pending", actor=self._actor())
        esc.save(update_fields=["status", "timeline", "updated_at"])
        return Response(self.get_serializer(esc).data)

    @action(detail=True, methods=["post"])
    def draft(self, request, pk=None):
        esc = self.get_object()
        esc.draft = request.data.get("draft") or request.data.get("body") or ""
        esc.save(update_fields=["draft", "updated_at"])
        return Response(self.get_serializer(esc).data)

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        esc = self.get_object()
        esc.status = Escalation.STATUS_RESOLVED
        esc.resolved_at = timezone.now()
        esc.resolved_by = self._actor()
        esc.add_event("resolved", actor=self._actor())
        esc.save(update_fields=["status", "resolved_at", "resolved_by", "timeline", "updated_at"])
        self._log(esc, "escalation_resolved", keyword=esc.matched_keyword)
        logger.info("ESCALATION_RESOLVED escalation=%s by=%s", esc.id, self._actor())
        return Response(self.get_serializer(esc).data)

    @action(detail=True, methods=["post"])
    def ignore(self, request, pk=None):
        esc = self.get_object()
        esc.status = Escalation.STATUS_IGNORED
        esc.resolved_at = timezone.now()
        esc.resolved_by = self._actor()
        esc.add_event("ignored", actor=self._actor())
        esc.save(update_fields=["status", "resolved_at", "resolved_by", "timeline", "updated_at"])
        self._log(esc, "escalation_ignored", keyword=esc.matched_keyword)
        logger.info("ESCALATION_IGNORED escalation=%s by=%s", esc.id, self._actor())
        return Response(self.get_serializer(esc).data)

    @action(detail=True, methods=["post"], url_path="create-ticket")
    def create_ticket(self, request, pk=None):
        """After manual review: create a HIGH-priority ticket from the escalation and remove it
        from the queue. The photos/videos the customer sent in the escalation thread are ALWAYS
        carried onto the new ticket. With notify=True (default) the customer is emailed a
        'ticket created' confirmation and the ticket is pushed to the Care Panel (with the media);
        with notify=False the ticket stays internal (no customer email) -- for pure legal cases.

        Note: a real Care Panel tracking LINK still requires an order id / phone; without one the
        confirmation email is sent WITHOUT a tracking link and the Care Panel push logs no_phone.
        """
        notify = str(request.data.get("notify", "true")).lower() not in ("false", "0", "no")
        esc = self.get_object()
        if esc.ticket_id:
            return Response(self.get_serializer(esc).data)
        ticket = Ticket.objects.create(            # ticket_id auto-assigned in Ticket.save()
            organization=esc.organization, brand=esc.brand, mailbox=esc.mailbox,
            customer_email=esc.sender, subject=esc.subject or "Escalation",
            issue_summary=esc.body[:2000], status=Ticket.STATUS_ESCALATED,
            priority=Ticket.PRIORITY_HIGH,
            extracted={"escalation": True, "matched_keyword": esc.matched_keyword})

        # Escalations skip ALL automation -> no phone/order was ever extracted. Pull them from the
        # escalation text (body + full conversation) so the Care Panel store can build a tracking
        # link (it is phone-keyed) and resolve the Shopify order owner.
        from apps.classifier.rule_classifier import _extract_order_id, _extract_phone
        texts = [esc.subject or "", esc.body or ""] + [
            c.get("body", "") for c in (esc.conversation or [])]
        combined = "\n".join(t for t in texts if t)
        phone = _extract_phone(combined)
        order_id = _extract_order_id(combined)
        if phone or order_id:
            ex = ticket.extracted or {}
            if phone:
                ex["phone"] = phone
            if order_id:
                ex["order_id"] = order_id
            ticket.extracted = ex
            ticket.save(update_fields=["extracted", "updated_at"])
            logger.info("ESCALATION_TICKET_EXTRACTED phone=%s order_id=%s",
                        phone or "-", order_id or "-")

        # Carry the escalation's media (original + customer-reply attachments) onto the ticket so
        # they show on the ticket and get uploaded to the Care Panel.
        has_photo = has_video = False
        for att in esc.reply_attachments.all():
            ct = (att.content_type or "").lower()
            att.ticket = ticket
            att.save(update_fields=["ticket"])
            has_photo = has_photo or ct.startswith("image/")
            has_video = has_video or ct.startswith("video/")
        if has_photo or has_video:
            ex = ticket.extracted or {}
            ex["has_media"] = True
            if has_photo:
                ex["has_photo"] = True
            if has_video:
                ex["has_video"] = True
            ticket.extracted = ex
            ticket.save(update_fields=["extracted", "updated_at"])

        AuditLogEntry.objects.create(
            ticket=ticket, actor=self._actor(), event="ticket_created",
            detail={"from_escalation": esc.id, "matched_keyword": esc.matched_keyword,
                    "notify": notify, "media_carried": esc.reply_attachments.count()})
        esc.ticket = ticket
        esc.status = Escalation.STATUS_TICKET_CREATED       # removed from the open queue
        esc.add_event("ticket_created", actor=self._actor(), ticket_id=ticket.ticket_id, notify=notify)
        esc.save(update_fields=["ticket", "status", "timeline", "updated_at"])
        logger.info("ESCALATION_TICKET_CREATED notify=%s media=%s",
                    notify, esc.reply_attachments.count())
        logger.info("TICKET_ID=%s", ticket.ticket_id)

        if notify:
            # send_confirmation -> creates the Care Panel ticket (tracking hash) + emails the
            # customer. _upload_care_panel_media then pushes the photos/videos to that ticket's
            # tracking page (the separate comment-form step) so they show under 'Media Files'.
            from apps.ingestion.service import _upload_care_panel_media, send_confirmation
            send_confirmation(ticket, "created")
            _upload_care_panel_media(ticket)
        ticket.refresh_from_db()
        return Response(self.get_serializer(esc).data)


from .models import InternalEmail  # noqa: E402
from .serializers import InternalEmailSerializer  # noqa: E402


class InternalEmailViewSet(OrgScopedViewSet):
    """The Internal Communications inbox (emails to internal addresses). Completely independent of
    tickets / escalations. Actions: reply / forward / mark_read / mark_unread / archive / delete /
    assign / note. NO ticket creation, NO customer automation."""

    queryset = InternalEmail.objects.select_related("brand").all()
    serializer_class = InternalEmailSerializer
    org_lookup = "organization"
    brand_lookup = "brand"
    search_fields = ["sender", "sender_name", "subject", "body", "assigned_to"]
    ordering_fields = ["created_at", "received_at", "status"]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params
        for field in ("status", "assigned_to"):
            if p.get(field):
                qs = qs.filter(**{field: p[field]})
        if p.get("since"):
            qs = qs.filter(created_at__date__gte=p["since"])
        return qs

    def _actor(self):
        u = self.request.user
        return getattr(u, "email", "") or getattr(u, "username", "") or "agent"

    def retrieve(self, request, *args, **kwargs):
        ie = self.get_object()
        if not ie.is_read:
            ie.is_read = True
            ie.save(update_fields=["is_read", "updated_at"])
        return Response(self.get_serializer(ie).data)

    @action(detail=False, methods=["get"])
    def unread_count(self, request):
        """Poll endpoint for the Internal Communications sidebar badge + new-item toasts: count of
        UNREAD internal emails (terminal archived/deleted excluded) plus the newest unread items
        (id / sender / name / subject). Read-only -- mirrors the escalation endpoint and never
        changes the internal-communications workflow."""
        base = (OrgScopedViewSet.get_queryset(self)
                .filter(is_read=False)
                .exclude(status__in=InternalEmail.TERMINAL_STATUSES))
        items = [{"id": e.id, "sender": e.sender, "sender_name": e.sender_name, "subject": e.subject}
                 for e in base.order_by("-created_at")[:25]]
        return Response({"count": base.count(), "items": items})

    def _files(self, request, ie):
        email_atts, stored = [], []
        for f in request.FILES.getlist("attachments"):
            content = f.read()
            att = Attachment.objects.create(
                internal_email=ie, filename=f.name, content_type=f.content_type or "",
                size=len(content), file=ContentFile(content, name=f.name))
            email_atts.append((f.name, content, f.content_type or "application/octet-stream"))
            stored.append({"filename": f.name, "content_type": f.content_type or "",
                           "url": f"/api/attachments/{att.id}/"})
        return email_atts, stored

    @action(detail=True, methods=["post"])
    def reply(self, request, pk=None):
        from apps.ingestion.service import send_internal_reply
        ie = self.get_object()
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response({"detail": "Body required."}, status=http_status.HTTP_400_BAD_REQUEST)
        email_atts, stored = self._files(request, ie)
        sent = send_internal_reply(ie, body, agent=self._actor(),
                                   subject=(request.data.get("subject") or "").strip() or None,
                                   email_attachments=email_atts, stored_attachments=stored,
                                   from_email=request.data.get("from_email", ""))
        ie.refresh_from_db()
        if not sent:
            data = self.get_serializer(ie).data
            data["send_failed"] = True
            data["detail"] = "Email could not be sent (SMTP failed). Saved so you can retry."
            return Response(data, status=http_status.HTTP_502_BAD_GATEWAY)
        return Response(self.get_serializer(ie).data)

    @action(detail=True, methods=["post"])
    def forward(self, request, pk=None):
        from apps.ingestion.service import send_internal_reply
        ie = self.get_object()
        to = (request.data.get("to") or "").strip()
        body = (request.data.get("body") or "").strip()
        if not to:
            return Response({"detail": "Recipient (to) required."}, status=http_status.HTTP_400_BAD_REQUEST)
        email_atts, stored = self._files(request, ie)
        sent = send_internal_reply(ie, body or ie.body, agent=self._actor(), to=to, forward=True,
                                   subject=(request.data.get("subject") or "").strip() or None,
                                   email_attachments=email_atts, stored_attachments=stored,
                                   from_email=request.data.get("from_email", ""))
        ie.refresh_from_db()
        if not sent:
            return Response({"detail": "Forward failed (SMTP)."}, status=http_status.HTTP_502_BAD_GATEWAY)
        return Response(self.get_serializer(ie).data)

    @action(detail=True, methods=["post"])
    def note(self, request, pk=None):
        from apps.ingestion.service import add_internal_note
        ie = self.get_object()
        text = (request.data.get("note") or request.data.get("body") or "").strip()
        if not text:
            return Response({"detail": "Note required."}, status=http_status.HTTP_400_BAD_REQUEST)
        add_internal_note(ie, text, agent=self._actor())
        ie.refresh_from_db()
        return Response(self.get_serializer(ie).data)

    def _set_read(self, request, read):
        ie = self.get_object()
        ie.is_read = read
        ie.save(update_fields=["is_read", "updated_at"])
        return Response(self.get_serializer(ie).data)

    @action(detail=True, methods=["post"], url_path="mark-read")
    def mark_read(self, request, pk=None):
        return self._set_read(request, True)

    @action(detail=True, methods=["post"], url_path="mark-unread")
    def mark_unread(self, request, pk=None):
        return self._set_read(request, False)

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        ie = self.get_object()
        agent = (request.data.get("assigned_to") or request.data.get("agent") or "").strip()
        if not agent:
            return Response({"detail": "assigned_to required."}, status=http_status.HTTP_400_BAD_REQUEST)
        ie.assigned_to = agent
        ie.assigned_at = timezone.now()
        ie.add_event("assigned", actor=self._actor(), assigned_to=agent)
        ie.save(update_fields=["assigned_to", "assigned_at", "timeline", "updated_at"])
        logger.info("INTERNAL-EMAIL-ASSIGNED internal=%s to=%s by=%s", ie.id, agent, self._actor())
        return Response(self.get_serializer(ie).data)

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        ie = self.get_object()
        ie.status = InternalEmail.STATUS_ARCHIVED
        ie.add_event("archived", actor=self._actor())
        ie.save(update_fields=["status", "timeline", "updated_at"])
        logger.info("INTERNAL-EMAIL-ARCHIVED internal=%s by=%s", ie.id, self._actor())
        return Response(self.get_serializer(ie).data)

    @action(detail=True, methods=["post"])
    def delete(self, request, pk=None):
        ie = self.get_object()
        ie.status = InternalEmail.STATUS_DELETED         # soft delete -> keeps audit history
        ie.add_event("deleted", actor=self._actor())
        ie.save(update_fields=["status", "timeline", "updated_at"])
        logger.info("INTERNAL-EMAIL-DELETED internal=%s by=%s", ie.id, self._actor())
        return Response(self.get_serializer(ie).data)
