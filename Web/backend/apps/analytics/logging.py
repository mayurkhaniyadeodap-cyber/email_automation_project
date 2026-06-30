"""
Reporting log helpers (ADDITIVE). Each emits a structured log line AND persists a row in the
reporting tables. All calls are best-effort and wrapped so a reporting failure can NEVER break
the underlying email / ticket / escalation workflow.
"""

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 -- reporting must never break the real flow
        logger.exception("analytics logging failed (non-fatal)")
        return None


def log_manual_reply(*, brand, employee=None, customer_email="", subject="", message_id="",
                     thread_id="", ticket=None, escalation=None, body="", attachments=0,
                     response_seconds=None, sender_email=""):
    """Record a MANUAL agent reply. Stores employee id/name/email + the ACTUAL sender (alias)
    From address + thread identifiers."""
    def _do():
        from .models import ManualReplyLog
        name = (getattr(employee, "get_full_name", lambda: "")() or getattr(employee, "username", "")) if employee else ""
        email = getattr(employee, "email", "") if employee else ""
        row = ManualReplyLog.objects.create(
            organization=brand.organization, brand=brand, ticket=ticket, escalation=escalation,
            employee=employee, employee_name=name, employee_email=email,
            sender_email=(sender_email or "").strip().lower(),
            customer_email=customer_email, subject=subject[:500], message_id=message_id,
            thread_id=thread_id, ticket_ref=getattr(ticket, "ticket_id", "") or "",
            reply_size=len(body or ""), attachments=attachments,
            response_seconds=response_seconds)
        logger.info("MANUAL_REPLY_SENT employee=%s sender=%s to=%s ticket=%s msg=%s size=%d",
                    email or "-", row.sender_email or "-", customer_email or "-",
                    row.ticket_ref or "-", message_id or "-", row.reply_size)
        if employee:
            touch_activity(employee)
        return row
    return _safe(_do)


def log_auto_reply(*, brand, customer_email="", subject="", template="", trigger="", ticket=None,
                   execution_ms=0, success=True):
    """Record an AUTOMATED reply (template / trigger / success / execution time)."""
    def _do():
        from .models import AutoReplyLog
        row = AutoReplyLog.objects.create(
            organization=brand.organization, brand=brand, ticket=ticket,
            customer_email=customer_email, subject=subject[:500], template=template,
            trigger=trigger, ticket_ref=getattr(ticket, "ticket_id", "") or "",
            execution_ms=execution_ms, success=success,
            status="sent" if success else "failed")
        logger.info("AUTO_REPLY_SENT template=%s trigger=%s to=%s ticket=%s success=%s",
                    template or "-", trigger or "-", customer_email or "-",
                    row.ticket_ref or "-", success)
        return row
    return _safe(_do)


def touch_activity(employee):
    def _do():
        from .models import EmployeeActivity
        EmployeeActivity.objects.update_or_create(
            employee=employee, defaults={"last_active_at": timezone.now()})
    return _safe(_do)


def _client_meta(request):
    ua = request.META.get("HTTP_USER_AGENT", "") if request else ""
    ip = ""
    if request:
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        ip = xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", "")
    low = ua.lower()
    browser = next((b for b in ("edg", "chrome", "firefox", "safari", "opera")
                    if b in low), "")
    device = "Mobile" if any(k in low for k in ("mobile", "android", "iphone")) else "Desktop"
    name = {"edg": "Edge", "chrome": "Chrome", "firefox": "Firefox",
            "safari": "Safari", "opera": "Opera"}.get(browser, ua[:60])
    return ip, device, name


def log_login(employee, request=None):
    def _do():
        from .models import EmployeeLoginHistory
        ip, device, browser = _client_meta(request)
        row = EmployeeLoginHistory.objects.create(
            employee=employee, login_at=timezone.now(), ip_address=ip, device=device,
            browser=browser)
        touch_activity(employee)
        logger.info("EMPLOYEE_LOGIN employee=%s ip=%s device=%s browser=%s",
                    getattr(employee, "email", "-"), ip or "-", device, browser)
        return row
    return _safe(_do)


def log_logout(employee):
    def _do():
        from .models import EmployeeLoginHistory
        row = (EmployeeLoginHistory.objects.filter(employee=employee, logout_at__isnull=True)
               .order_by("-login_at").first())
        if row:
            row.logout_at = timezone.now()
            if row.login_at:
                row.session_seconds = int((row.logout_at - row.login_at).total_seconds())
            row.save(update_fields=["logout_at", "session_seconds", "updated_at"])
        logger.info("EMPLOYEE_LOGOUT employee=%s", getattr(employee, "email", "-"))
        return row
    return _safe(_do)
