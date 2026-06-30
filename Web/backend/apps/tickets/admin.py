from django.contrib import admin

from .models import (
    AuditLogEntry, Escalation, Inquiry, Message, PendingConversation, Ticket)


@admin.register(Escalation)
class EscalationAdmin(admin.ModelAdmin):
    list_display = ("matched_keyword", "sender", "subject", "status", "priority", "queue",
                    "received_at", "created_at")
    list_filter = ("brand", "status", "priority", "queue", "matched_keyword")
    search_fields = ("sender", "subject", "matched_keyword")


@admin.register(Inquiry)
class InquiryAdmin(admin.ModelAdmin):
    list_display = ("inquiry_type", "status", "customer_email", "customer_name", "phone",
                    "queue", "channel", "created_at")
    list_filter = ("brand", "inquiry_type", "status", "channel", "queue")
    search_fields = ("customer_email", "customer_name", "phone")
    readonly_fields = ("data",)


@admin.register(PendingConversation)
class PendingConversationAdmin(admin.ModelAdmin):
    list_display = (
        "customer_email", "subject", "category", "order_id", "phone",
        "evidence_requests", "created_at",
    )
    search_fields = ("customer_email", "subject", "order_id", "phone")
    list_filter = ("brand", "category")


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0


class AuditLogInline(admin.TabularInline):
    model = AuditLogEntry
    extra = 0


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        "ticket_id", "brand", "subject", "category", "status",
        "classification_status", "priority", "is_ignored", "created_at",
    )
    list_filter = ("brand", "status", "classification_status", "priority", "is_ignored")
    search_fields = ("ticket_id", "subject", "customer_email", "ai_error")
    readonly_fields = ("classification_status", "ai_error", "ai_attempts", "ai_confidence")
    inlines = [MessageInline, AuditLogInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("ticket", "direction", "from_email", "is_draft", "created_at")
    list_filter = ("direction", "is_draft")


@admin.register(AuditLogEntry)
class AuditLogEntryAdmin(admin.ModelAdmin):
    """AI errors and all ticket events, visible/searchable in admin (spec rule 6)."""
    list_display = ("created_at", "ticket", "actor", "event")
    list_filter = ("event", "actor")
    search_fields = ("ticket__ticket_id", "event", "detail")
