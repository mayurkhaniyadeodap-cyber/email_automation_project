"""Read-only list of held PendingConversations (waiting for evidence/video)."""

from rest_framework import serializers, viewsets  # type: ignore

from apps.tickets.models import PendingConversation


class PendingConversationSerializer(serializers.ModelSerializer):
    class Meta:
        model = PendingConversation
        # body_text/body_html + thread ids let the panel open a READ-ONLY conversation
        # for a held (verification-failed / awaiting-evidence) email -- it has no Ticket
        # and no Message rows, so its content lives on the pending record itself.
        fields = ["id", "customer_email", "phone", "order_id", "subject", "category",
                  "sub_topic", "issue_summary", "status", "evidence_requests",
                  "body_text", "body_html", "language", "original_message_id",
                  "last_message_id", "thread_id", "has_evidence", "has_photo",
                  "has_video", "requires_evidence", "created_at", "updated_at"]


class PendingConversationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PendingConversationSerializer
    search_fields = ["customer_email", "subject", "order_id", "phone"]
    ordering_fields = ["created_at", "updated_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = PendingConversation.objects.all()
        user = self.request.user
        if not user.is_superuser:
            qs = qs.filter(organization__in=user.organizations.all())
        params = self.request.query_params
        if params.get("organization"):
            qs = qs.filter(organization=params["organization"])
        if params.get("brand"):
            qs = qs.filter(brand=params["brand"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        elif params.get("include_closed", "").lower() != "true":
            # The Pending tab shows ACTIVE held conversations -- a CLOSED pending (a finished
            # inquiry / promoted ticket) is not "waiting" for anything, so hide it by default.
            qs = qs.exclude(status="closed")
        return qs
