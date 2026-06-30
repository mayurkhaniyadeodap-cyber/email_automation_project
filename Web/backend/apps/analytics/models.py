"""
Reporting / dashboard data layer (ADDITIVE -- no existing workflow reads or writes these).
Five logging tables feed the manager dashboard, agent performance, and the reply reports:
manual_reply_logs, auto_reply_logs, employee_activity, employee_login_history,
dashboard_daily_stats.
"""

from django.conf import settings
from django.db import models

from apps.common import TimestampedModel
from apps.organizations.models import Brand, Organization


class ManualReplyLog(TimestampedModel):
    """One row per MANUAL agent reply (ticket or escalation). Captures who replied, to whom,
    and the thread identifiers (req: store employee id/name/email, message id, thread id...)."""

    class Meta:
        db_table = "manual_reply_logs"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["brand", "created_at"]),
                   models.Index(fields=["employee_email"])]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="+")
    ticket = models.ForeignKey("tickets.Ticket", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="manual_reply_logs")
    escalation = models.ForeignKey("tickets.Escalation", on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="manual_reply_logs")
    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                 blank=True, related_name="+")
    employee_name = models.CharField(max_length=200, blank=True, default="")
    employee_email = models.EmailField(blank=True, default="")
    # The ACTUAL From address the reply was sent as (the support inbox or a 'send mail as' alias),
    # NOT the login/admin email. Employee attribution stays on `employee`, so replies from any
    # alias still count for the same employee.
    sender_email = models.EmailField(blank=True, default="")
    customer_email = models.EmailField(blank=True, default="")
    subject = models.CharField(max_length=500, blank=True, default="")
    message_id = models.CharField(max_length=255, blank=True, default="")
    thread_id = models.CharField(max_length=255, blank=True, default="")
    ticket_ref = models.CharField(max_length=60, blank=True, default="")
    reply_size = models.PositiveIntegerField(default=0)
    attachments = models.PositiveIntegerField(default=0)
    response_seconds = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=40, blank=True, default="sent")


class AutoReplyLog(TimestampedModel):
    """One row per AUTOMATED reply the engine sends (template / trigger / success)."""

    class Meta:
        db_table = "auto_reply_logs"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["brand", "created_at"])]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="+")
    ticket = models.ForeignKey("tickets.Ticket", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="auto_reply_logs")
    customer_email = models.EmailField(blank=True, default="")
    subject = models.CharField(max_length=500, blank=True, default="")
    template = models.CharField(max_length=120, blank=True, default="")
    trigger = models.CharField(max_length=120, blank=True, default="")
    ticket_ref = models.CharField(max_length=60, blank=True, default="")
    execution_ms = models.PositiveIntegerField(default=0)
    success = models.BooleanField(default=True)
    status = models.CharField(max_length=40, blank=True, default="sent")


class EmployeeActivity(TimestampedModel):
    """Rolling 'last active' marker per employee (touched on every agent action)."""

    class Meta:
        db_table = "employee_activity"

    employee = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                    related_name="activity")
    last_active_at = models.DateTimeField(null=True, blank=True)


class EmployeeLoginHistory(TimestampedModel):
    """Login / logout sessions: time in/out, duration, IP, device, browser."""

    class Meta:
        db_table = "employee_login_history"
        ordering = ["-login_at"]

    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                 related_name="login_history")
    login_at = models.DateTimeField(null=True, blank=True)
    logout_at = models.DateTimeField(null=True, blank=True)
    session_seconds = models.PositiveIntegerField(null=True, blank=True)
    ip_address = models.CharField(max_length=64, blank=True, default="")
    device = models.CharField(max_length=120, blank=True, default="")
    browser = models.CharField(max_length=200, blank=True, default="")


class DashboardDailyStats(TimestampedModel):
    """Per-brand per-day rollup (rebuilt on demand) -- powers the trend charts cheaply."""

    class Meta:
        db_table = "dashboard_daily_stats"
        ordering = ["-day"]
        constraints = [models.UniqueConstraint(fields=["brand", "day"], name="uniq_brand_day")]

    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="+")
    day = models.DateField()
    emails_received = models.PositiveIntegerField(default=0)
    tickets_created = models.PositiveIntegerField(default=0)
    tickets_resolved = models.PositiveIntegerField(default=0)
    auto_replies = models.PositiveIntegerField(default=0)
    manual_replies = models.PositiveIntegerField(default=0)
    escalations = models.PositiveIntegerField(default=0)
